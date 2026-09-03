# Models

ImgGuard uses two ONNX models, mirrored on this repository's releases:
https://github.com/codedbyjake/ImgGuard/releases/tag/models-v1

## image-safety-classifier-l.onnx

Copyright (c) Owen Elliott. Licensed under the MIT License.
Source: https://huggingface.co/OwenElliott/image-safety-classifier-l

Redistributed unmodified.

## vit-mature-content-detection-int8.onnx

Copyright (c) prithivMLmods. Licensed under the Apache License 2.0, available at
https://www.apache.org/licenses/LICENSE-2.0

Source: https://huggingface.co/prithivMLmods/Vit-Mature-Content-Detection

This file has been changed from the upstream work. The upstream weights are
published as PyTorch `safetensors`; they were exported to ONNX and then
quantised to uint8 weights.
