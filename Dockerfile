# One image for every role. Dependencies are installed as wheels into /deps with the same
# Python minor version the distroless runtime ships (3.13), so they import there unchanged.
FROM docker.io/library/python:3.13-slim-trixie AS build
COPY requirements.txt /tmp/requirements.txt
# Hashes pinned in requirements.txt; wheels only, so nothing is compiled.
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --target /deps -r /tmp/requirements.txt
COPY platform_probe /app/platform_probe
# Byte-compile now: the root filesystem is read-only at run time.
RUN python -m compileall -q /deps /app

# distroless: no shell, no package manager, CA certificates included. Runs as any UID, which
# is what OpenShift's restricted-v2 SCC assigns.
FROM gcr.io/distroless/python3-debian13:nonroot
ARG VERSION=dev
COPY --from=build /deps /deps
COPY --from=build /app /app
ENV PYTHONPATH=/app:/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_PROBE_VERSION=$VERSION
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/bin/python3", "-m", "platform_probe"]
