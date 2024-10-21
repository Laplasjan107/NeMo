# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
import os
import shutil
from typing import Optional, Union

import torch
import torch.distributed as dist
from datasets import load_dataset

from nemo.collections import llm
from nemo.collections.nlp.parts.utils_funcs import torch_dtype_from_precision
from nemo.utils import logging
from .utils import get_unwrapped_mcore_model

try:
    import modelopt.torch.quantization as mtq
    from modelopt.torch.export import export_tensorrt_llm_checkpoint

    QUANT_CFG_CHOICES = {
        "int8": mtq.INT8_DEFAULT_CFG,
        "int8_sq": mtq.INT8_SMOOTHQUANT_CFG,
        "fp8": mtq.FP8_DEFAULT_CFG,
        "int4_awq": mtq.INT4_AWQ_CFG,
        "w4a8_awq": mtq.W4A8_AWQ_BETA_CFG,
        "int4": mtq.INT4_BLOCKWISE_WEIGHT_ONLY_CFG,
    }

    HAVE_MODELOPT = True

except (ImportError, ModuleNotFoundError) as e:
    HAVE_MODELOPT = False
    HAVE_MODELOPT_ERROR = e


SUPPORTED_DTYPE = [16, "16", "bf16"]  # Default precision for non-quantized layers

@dataclass
class QuantizationConfig:
    algorithm: Optional[str] = "fp8" # one of QUANT_CFG_CHOICES keys
    awq_block_size: int = 128
    sq_alpha: float = 0.5
    enable_kv_cache: bool = True

@dataclass
class ExportConfig:
    path: str
    dtype: Union[str, int] = "bf16"
    decoder_type: Optional[str] = None
    inference_tensor_parallel: int = 1
    inference_pipeline_parallel: int = 1


def get_modelopt_decoder_type(config: llm.GPTConfig) -> str:
    """Infers the modelopt decoder type from GPTConfig class"""
    mapping = [
        (llm.Baichuan2Config, "baichuan"),
        (llm.ChatGLMConfig, "chatglm"),
        (llm.GemmaConfig, "gemma"),
        (llm.LlamaConfig, "llama"),
        (llm.MistralConfig7B, "llama"),
        (llm.MixtralConfig, "llama"),
        (llm.NemotronConfig, "gptnext"),
        (llm.Qwen2Config, "qwen"),
        # TODO: (llm.StarcoderConfig,   ""),
        (llm.Starcoder2Config, "gptnext"),
    ]

    for config_class, decoder_type in mapping:
        if isinstance(config, config_class):
            return decoder_type

    logging.warning("Could not directly infer the decoder type")
    # TODO: Add a reasonable behavior for GPTConfig (for instance based on position_embedding_type)
    return "llama"


class Quantizer:
    """Post-training quantization (PTQ) and TRT-LLM export of NeMo 2.0 checkpoints.

    PTQ converts selected model layers to low-precision format (e.g., INT4, FP8) for efficient serving.
    The process consist of several steps:

        1. Loading a Nemo model from disk using appropriate parallelism strategy
        2. Calibrating the model to obtain appropriate algorithm-specific scaling factors
        3. Producing output directory

    The output directory produced is intended to be consumed by TensorRT-LLM toolbox
    for efficient inference. This can be achieved using NeMo inference containers.

    Available quantization methods are listed in `QUANT_CFG_CHOICES` dictionary above.
    Please consult Model Optimizer documentation https://nvidia.github.io/TensorRT-Model-Optimizer/ for details.

    Quantization algorithm can also be conveniently set to None to perform only weights export step
    for TensorRT-LLM deployment. This is useful to getting baseline results for a full-precision model.
    """

    def __init__(self, quantization_config: QuantizationConfig, export_config: ExportConfig):
        """Initialize Quantizer with quantization and export configurations. """

        if not HAVE_MODELOPT:
            raise RuntimeError("nvidia-modelopt is needed to use Quantizer") from HAVE_MODELOPT_ERROR
        if not torch.cuda.is_available():
            raise EnvironmentError("GPU is required for the quantization.")

        self.quantization_config: QuantizationConfig = quantization_config
        self.export_config: ExportConfig = export_config
        self.nemo_checkpoint_path = None

        algorithm = quantization_config.algorithm
        dtype = export_config.dtype

        # Export and Quantization config sanity checks
        assert algorithm is None or algorithm in QUANT_CFG_CHOICES, f"Unsupported quantization algorithm: {algorithm}"
        if export_config is not None:
            assert dtype in SUPPORTED_DTYPE, f"Unsupported export dtype: {dtype}"
        self.torch_dtype = torch_dtype_from_precision(dtype)


    def _setup(self, model: llm.GPTModel) -> None:
        """Setup model for quantization."""
        # TODO: disable activation checkpointing
        model.config.vocab_size = model.tokenizer.vocab_size
        model.config.pipeline_dtype = self.torch_dtype  # TODO: for some reason model.pipeline.dtype does not work
        model.freeze()


    def _get_decoder_type(self, config: llm.GPTConfig):
        return self.export_config.decoder_type or get_modelopt_decoder_type(config)


    def quantize(self, model: llm.GPTModel, forward_loop):
        """Quantize the model and calibrate using given forward loop."""
        algorithm = self.quantization_config.algorithm
        if algorithm is None:
            logging.info("Quantization algorithm set to None, returning the non-quantized model")
            return model

        logging.info(f"Quantizing model to {algorithm}...")

        self._setup(model)
        unwrapped_model = get_unwrapped_mcore_model(model)
        decoder_type = self._get_decoder_type(unwrapped_model.config)
        quant_cfg = QUANT_CFG_CHOICES[algorithm]
        if "awq" in algorithm:
            weight_quantizer = quant_cfg["quant_cfg"]["*weight_quantizer"]
            if isinstance(weight_quantizer, list):
                weight_quantizer = weight_quantizer[0]
            weight_quantizer["block_sizes"][-1] = self.quantization_config.awq_block_size

        # Always turn on FP8 kv cache to save memory footprint.
        # For int8_sq, we use int8 kv cache.
        # TODO: Investigate why enabling FP8 kv cache will cause accuracy regressions for Nemotron.
        enable_quant_kv_cache = self.quantization_config.enable_kv_cache
        if enable_quant_kv_cache is None:
            enable_quant_kv_cache = "int8" not in algorithm and decoder_type != "gptnext"
        logging.info(f'{"Enabled" if enable_quant_kv_cache else "Disabled"} KV cache quantization')
        quant_cfg["quant_cfg"]["*output_quantizer"] = {
            "num_bits": 8 if algorithm == "int8_sq" else (4, 3),
            "axis": None,
            "enable": enable_quant_kv_cache,
        }
        if algorithm == "int8_sq":
            sq_alpha = self.quantization_config.sq_alpha
            logging.info(f"Using int8_sq alpha = {sq_alpha}")
            quant_cfg["algorithm"] = {"method": "smoothquant", "alpha": sq_alpha}

        unwrapped_model = mtq.quantize(unwrapped_model, quant_cfg, forward_loop)

        if decoder_type == "gptnext":
            # We found squared_relu may have an under-calibration problem.
            # Clamp the scaling_factor with a min threshold to avoid under-calibration.
            match algorithm:
                case "fp8":
                    maxbound = 448
                case "int8_sq":
                    maxbound = 127
                case _:
                    maxbound = 0

            unwrapped_model = mtq.postprocess_amax(
                unwrapped_model, "*input_quantizer", lambda amax: torch.clamp(amax, min=0.01 * maxbound)
            )

        if dist.get_rank() == 0:
            mtq.print_quant_summary(unwrapped_model)

        return model


    def create_megatron_forward_loop(
        self, get_dataloader, num_batches, seq_length=None, micro_batch_size=None, decoder_seq_length=None
    ):
        from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

        forward_backward_func = get_forward_backward_func()

        def forward_step_func(data_iterator, model):
            data = next(data_iterator)
            batch_len, seq_len = data.shape
            position_ids = torch.arange(seq_len, device=data.device).expand((batch_len, seq_len))
            output_tensor = model(data, position_ids, None)

            def _mock_loss_function(tensor):
                return 0, {}

            return output_tensor, _mock_loss_function

        def loop(model):
            dataloader = get_dataloader()
            forward_backward_func(
                forward_step_func=forward_step_func,
                data_iterator=dataloader,
                model=model,
                num_microbatches=num_batches,
                seq_length=seq_length,
                micro_batch_size=micro_batch_size,
                decoder_seq_length=decoder_seq_length,
                forward_only=True,
            )

        return loop


    def export(self, model: llm.GPTModel, nemo_checkpoint_path: Optional[str] = None) -> None:
        assert self.export_config is not None, "Export config is not set"
        # TODO: Add sample generate
        # TODO: Support NeMo 2:
        # if model.cfg.megatron_amp_O2:
        #     model.model = unwrap_model(model.model, Float16Module)
        export_dir = self.export_config.path
        use_nfs_workspace = (model.trainer._fabric.__io__.num_nodes > 1) or (
            model.config.pipeline_model_parallel_size > 1
        )
        export_tensorrt_llm_checkpoint(
            model=get_unwrapped_mcore_model(model),
            decoder_type=self._get_decoder_type(model.config),
            dtype=self.torch_dtype,
            export_dir=export_dir,
            inference_tensor_parallel=self.export_config.inference_tensor_parallel,
            inference_pipeline_parallel=self.export_config.inference_pipeline_parallel,
            use_nfs_workspace=use_nfs_workspace,
        )

        dist.barrier()  # Wait until all ranks complete export_model_config step
        logging.info(f"Export succeeded, model has been exported to {export_dir}. Saving tokenizer if possible...")

        if dist.get_rank() == 0:
            self.nemo_checkpoint_path = nemo_checkpoint_path or self.nemo_checkpoint_path
            if self.nemo_checkpoint_path is not None:
                tokenizer_src = os.path.join(self.nemo_checkpoint_path, 'context', 'nemo_tokenizer')
                tokenizer_dst = os.path.join(export_dir, 'tokenizer')

                if os.path.exists(tokenizer_src) and not os.path.exists(tokenizer_dst):
                    shutil.copytree(tokenizer_src, tokenizer_dst)
                else:
                    logging.info("Could not copy tokenizer from the NeMo checkpoint")


def get_calib_data_iter(
    data: str = "cnn_dailymail", batch_size: int = 64, calib_size: int = 512, max_sequence_length: int = 512
):
    """Creates a sample data iterator for calibration"""
    if data == "wikitext":
        dataset = load_dataset("wikitext", "wikitext-103-v1", split="train")
        text_column = "text"
    elif data == "cnn_dailymail":
        dataset = load_dataset("cnn_dailymail", name="3.0.0", split="train")
        text_column = "article"
    else:
        # Assume a local JSON dataset with a column named "text"
        dataset = load_dataset("json", data_files=data, split="train")
        text_column = "text"
    calib_size = max(min(len(dataset), calib_size), batch_size)
    for i in range(calib_size // batch_size):
        batch = dataset[i * batch_size : (i + 1) * batch_size][text_column]
        for j in range(len(batch)):
            batch[j] = batch[j][:max_sequence_length]
        yield batch
