# MediaPipe Models

Place the optional local MediaPipe Face Landmarker model here:

```text
backend/models/face_landmarker.task
```

`ps_generate_face_selection` stays available without the model, but returns a structured
`face_landmarker_model_missing` error until the file is present. The model runs locally;
it is not a paid API call.

## SAM 2.1

Place the optional local SAM 2.1 Base+ checkpoint here:

```text
backend/models/sam2/sam2.1_hiera_base_plus.pt
```

The SAM worker runs from its own environment at `D:\Photo_sontrol\.venv-sam`
and listens on `127.0.0.1:17861` when started with:

```powershell
python backend\cli.py sam start
```

`ps_generate_sam_mask` remains registered when the model is missing, but returns
a structured `sam_worker_unreachable`, `sam_start_prerequisites_missing`, or
`sam_model_missing` diagnostic until the worker and checkpoint are ready.

## Grounding DINO + HQ-SAM

Place the optional local Grounding DINO checkpoint here:

```text
backend/models/grounding_dino/groundingdino_swint_ogc.pth
```

The repo already includes a local Grounding DINO config at:

```text
backend/models/grounding_dino/GroundingDINO_SwinT_OGC.py
```

Place the HQ-SAM checkpoint here:

```text
backend/models/sam_hq/sam_hq_vit_l.pth
```

The Grounding DINO + HQ-SAM worker uses the same isolated environment
`D:\Photo_sontrol\.venv-sam` and listens on `127.0.0.1:17862` when started with:

```powershell
python backend\cli.py grounding start
```

`ps_detect_grounding_boxes`, `ps_generate_hqsam_mask`, and
`ps_generate_grounded_hq_mask` remain registered when the worker, dependencies,
or checkpoints are missing, but return structured
`grounding_hq_worker_unreachable`, `grounding_hq_start_prerequisites_missing`,
`grounding_dino_model_missing`, `grounding_dino_config_missing`, or
`hqsam_model_missing` diagnostics until the local worker is ready.

### Device policy

The worker uses a mixed-device policy by default:

```text
PS_AGENT_GROUNDING_DEVICE=auto
PS_AGENT_HQSAM_DEVICE=auto
```

`GroundingDINO` only uses CUDA when both `torch.cuda.is_available()` and the
compiled `groundingdino._C` extension are available. If `_C` is missing, `auto`
falls back to CPU and reports `grounding_cpu_fallback_no_cuda_extension` in
tool warnings. This keeps semantic detection usable without compiling the
custom CUDA op.

`HQ-SAM` stays GPU-first under `auto`; if CUDA is unavailable it falls back to
CPU and loads the checkpoint with CPU `map_location`.

Use `PS_AGENT_GROUNDING_DEVICE=cuda` only after compiling `groundingdino._C`.
If CUDA is forced while `_C` is unavailable, the worker returns
`grounding_cuda_extension_missing` instead of silently falling back.
