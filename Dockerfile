FROM alpine:3.22 AS ffmpeg-source
RUN apk add --no-cache ffmpeg

FROM n8nio/n8n:latest
USER root
COPY --from=ffmpeg-source /usr/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-source /usr/bin/ffprobe /usr/local/bin/ffprobe
COPY --from=ffmpeg-source /usr/lib/ /usr/lib/
USER node
