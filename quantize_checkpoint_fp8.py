#!/usr/bin/env python3
"""quantize_checkpoint_fp8.py -- build a native fp8_e4m3fn diffusion-model
checkpoint from a bf16 source, quantizing the same set of Linear weight
layers an existing int8 checkpoint of the same architecture quantizes (used
as a "which layers matter" oracle, rather than guessing a selection rule).

Writes ComfyUI's legacy "scaled_fp8" checkpoint format: a
"<prefix>scaled_fp8" marker tensor plus, per quantized layer,
"<layer>.weight" (fp8_e4m3fn) and "<layer>.scale_weight" (a per-tensor
float32 scale). `comfy.utils.convert_old_quants()` -- already called on
every normal checkpoint load (see comfy/sd.py) -- converts this into the
newer comfy_quant representation automatically; nothing here hand-crafts
that format.

Streams tensor-by-tensor via safetensors' lazy reader and writes the
output file manually (header computed up front from shapes/dtypes, then
tensor bytes appended in a single pass) rather than building the whole
output dict in memory at once -- the source file alone is 42GB.

Usage:
    quantize_checkpoint_fp8.py <source.safetensors> <oracle.safetensors> <output.safetensors>
        oracle: an existing quantized checkpoint of the same architecture;
        its I8-dtype tensor keys are used as the "quantize this layer" set.
"""

import json
import logging
import struct
import sys
import time
from pathlib import Path
from typing import BinaryIO

import torch
from safetensors import safe_open

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

F8_E4M3_MAX = 448.0
DTYPE_SIZES = {"BF16": 2, "F16": 2, "F32": 4, "F64": 8, "I8": 1, "U8": 1, "F8_E4M3": 1, "F8_E5M2": 1}


class Fp8CheckpointBuilder:
    """Builds a legacy-format scaled-fp8 checkpoint from a bf16 source,
    quantizing the layers an oracle (existing quantized) checkpoint
    quantizes.
    """

    def __init__(self, source_path: Path, oracle_path: Path, output_path: Path) -> None:
        self._source_path = source_path
        self._oracle_path = oracle_path
        self._output_path = output_path
        self._phase_timings: dict[str, float] = {}

    def build(self) -> dict[str, float]:
        """Run the full conversion. Returns a dict of phase-name -> seconds."""
        t_total0 = time.time()

        t0 = time.time()
        source_header = self._read_header(self._source_path)
        quantized_keys = self._oracle_quantized_keys(self._oracle_path, source_header)
        self._phase_timings["plan"] = time.time() - t0
        logger.info(
            "Plan: %d total tensors, %d will be quantized to fp8_e4m3fn (%.2fs)",
            len(source_header) - 1,
            len(quantized_keys),
            self._phase_timings["plan"],
        )

        t0 = time.time()
        model_prefix = self._detect_model_prefix(source_header)
        output_plan = self._compute_output_plan(source_header, quantized_keys, model_prefix)
        self._phase_timings["header_layout"] = time.time() - t0
        logger.info("Header layout computed in %.2fs", self._phase_timings["header_layout"])

        t0 = time.time()
        self._write_checkpoint(output_plan, quantized_keys, source_header.get("__metadata__"))
        self._phase_timings["write"] = time.time() - t0
        logger.info("Tensor data written in %.2fs", self._phase_timings["write"])

        self._phase_timings["total"] = time.time() - t_total0
        logger.info("Done in %.2fs total", self._phase_timings["total"])
        return self._phase_timings

    def _read_header(self, path: Path) -> dict[str, object]:
        with open(path, "rb") as f:
            header_len = struct.unpack("<Q", f.read(8))[0]
            return json.loads(f.read(header_len))

    def _oracle_quantized_keys(
        self, oracle_path: Path, source_header: dict[str, object]
    ) -> set[str]:
        """Keys the oracle checkpoint stores as I8, that also exist in the
        source with a matching shape (validated -- don't silently skip a
        mismatch, fail loudly instead).
        """
        oracle_header = self._read_header(oracle_path)
        candidates = {
            k
            for k, v in oracle_header.items()
            if k != "__metadata__" and v.get("dtype") == "I8"
        }
        missing = [k for k in candidates if k not in source_header]
        if missing:
            raise ValueError(f"{len(missing)} oracle-quantized keys missing from source, e.g. {missing[:3]}")
        mismatched = [
            k for k in candidates if source_header[k]["shape"] != oracle_header[k]["shape"]
        ]
        if mismatched:
            raise ValueError(f"{len(mismatched)} shape mismatches vs oracle, e.g. {mismatched[:3]}")
        return candidates

    def _detect_model_prefix(self, source_header: dict[str, object]) -> str:
        sample_key = next(k for k in source_header if k != "__metadata__")
        # ComfyUI diffusion-model checkpoints key everything under
        # "model.diffusion_model." -- confirmed against this exact file.
        prefix = "model.diffusion_model."
        if not sample_key.startswith(prefix):
            raise ValueError(f"unexpected key prefix, first key was {sample_key!r}")
        return prefix

    def _compute_output_plan(
        self, source_header: dict[str, object], quantized_keys: set[str], model_prefix: str
    ) -> list[dict[str, object]]:
        """One entry per output tensor: name, dtype, shape, source info
        needed to produce it, and its byte size (for offset computation).
        Order is deterministic (sorted) so header offsets and the later
        write pass agree without needing two full header rebuilds.
        """
        plan = []
        for key in sorted(k for k in source_header if k != "__metadata__"):
            info = source_header[key]
            if key in quantized_keys:
                nbytes = self._nelements(info["shape"])  # fp8: 1 byte/element
                plan.append({"name": key, "dtype": "F8_E4M3", "shape": info["shape"], "kind": "quantize", "nbytes": nbytes})
                scale_name = key[: -len(".weight")] + ".scale_weight" if key.endswith(".weight") else key + ".scale_weight"
                plan.append({"name": scale_name, "dtype": "F32", "shape": [1], "kind": "scale_for", "source_key": key, "nbytes": 4})
            else:
                nbytes = self._nelements(info["shape"]) * DTYPE_SIZES[info["dtype"]]
                plan.append({"name": key, "dtype": info["dtype"], "shape": info["shape"], "kind": "copy", "nbytes": nbytes})

        marker_name = f"{model_prefix}scaled_fp8"
        plan.append({"name": marker_name, "dtype": "F8_E4M3", "shape": [1], "kind": "marker", "nbytes": 1})
        return plan

    @staticmethod
    def _nelements(shape: list[int]) -> int:
        n = 1
        for d in shape:
            n *= d
        return n

    @staticmethod
    def _tensor_bytes(t: torch.Tensor) -> bytes:
        """Raw little-endian bytes for any torch dtype, including ones
        numpy has no native representation for (bfloat16, float8_e4m3fn)
        -- .numpy() alone raises on those, so reinterpret as uint8 first.
        """
        return t.reshape(-1).contiguous().view(torch.uint8).numpy().tobytes()

    def _write_checkpoint(
        self,
        plan: list[dict[str, object]],
        quantized_keys: set[str],
        metadata: dict[str, str] | None,
    ) -> None:
        header: dict[str, object] = {}
        if metadata is not None:
            # Carries the model's own architecture config (e.g. LTX-2.5's
            # AVTransformer3DModel spec -- layer/head counts etc.) that
            # ComfyUI's loader needs to instantiate the right module before
            # loading weights into it. Dropping this silently produces a
            # file whose tensor data is fine but gets loaded into the
            # wrong-shaped model (confirmed the hard way: a first attempt
            # without this produced "shape mismatch" errors on load even
            # though every tensor here was byte-identical to the source).
            header["__metadata__"] = metadata
        offset = 0
        for entry in plan:
            header[entry["name"]] = {
                "dtype": entry["dtype"],
                "shape": entry["shape"],
                "data_offsets": [offset, offset + entry["nbytes"]],
            }
            offset += entry["nbytes"]
        header_bytes = json.dumps(header).encode("utf-8")
        # safetensors requires 8-byte alignment padding on the header
        pad = (-len(header_bytes)) % 8
        header_bytes += b" " * pad

        with safe_open(self._source_path, framework="pt") as source, open(
            self._output_path, "wb"
        ) as out:
            out.write(struct.pack("<Q", len(header_bytes)))
            out.write(header_bytes)

            n_quantized = 0
            t_quant_total = 0.0
            n_copied = 0
            t_copy_total = 0.0
            t_last_log = time.time()

            for entry in plan:
                if entry["kind"] == "quantize":
                    t0 = time.time()
                    w = source.get_tensor(entry["name"]).to(torch.float32)
                    scale = (w.abs().amax() / F8_E4M3_MAX).clamp(min=1e-12)
                    q = (w / scale).to(torch.float8_e4m3fn)
                    out.write(self._tensor_bytes(q))
                    self._last_scale = scale  # consumed by the paired scale_for entry next
                    t_quant_total += time.time() - t0
                    n_quantized += 1
                elif entry["kind"] == "scale_for":
                    out.write(self._tensor_bytes(self._last_scale.to(torch.float32)))
                elif entry["kind"] == "copy":
                    t0 = time.time()
                    t = source.get_tensor(entry["name"])
                    out.write(self._tensor_bytes(t))
                    t_copy_total += time.time() - t0
                    n_copied += 1
                elif entry["kind"] == "marker":
                    out.write(self._tensor_bytes(torch.zeros(1, dtype=torch.float8_e4m3fn)))

                if time.time() - t_last_log > 15:
                    logger.info(
                        "progress: %d/%d quantized (%.1fs total), %d/%d copied (%.1fs total)",
                        n_quantized, len(quantized_keys), t_quant_total,
                        n_copied, len(plan) - len(quantized_keys) * 2 - 1, t_copy_total,
                    )
                    t_last_log = time.time()

        logger.info(
            "Final: %d layers quantized (%.2fs, %.1fms/layer avg), %d tensors copied through (%.2fs)",
            n_quantized, t_quant_total, (t_quant_total / n_quantized * 1000) if n_quantized else 0,
            n_copied, t_copy_total,
        )

    @classmethod
    def main(cls, argv: list[str] | None = None) -> None:
        argv = argv if argv is not None else sys.argv[1:]
        if len(argv) != 3:
            print(__doc__)
            sys.exit(1)
        source, oracle, output = (Path(p) for p in argv)
        timings = cls(source, oracle, output).build()
        print(json.dumps(timings, indent=2))


if __name__ == "__main__":
    Fp8CheckpointBuilder.main()
