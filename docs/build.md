# Building the image

Official images are published as
`ghcr.io/sturgeontechnologies/rcracehq-lapcounter` (public, `linux/arm64`).
Each release is tagged `vX.Y.Z`; builds between releases are `sha-<7 chars>`.
A tag is never re-pushed, so a tag always means the same image.

Pick a tag and set it as `lapcounter_app_image_tag` in
[RCRaceHQ-LapCounter-Install](https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-Install).

## Build your own

```bash
cd app
pip install -e '.[test]' && pytest -q
docker buildx build --platform linux/arm64 -t lapcounter:dev .   # QEMU on x86, native on arm64
docker compose up --build                                        # web on :8080, lapd → 127.0.0.1:5556
```

To run your own build on a Pi, push it to a registry you control and set
`lapcounter_app_image_repository` / `lapcounter_app_image_tag` in the Install
repo's inventory (or `docker save | ssh pi docker load` and
`lapcounter_app_registry_auth: none`).
