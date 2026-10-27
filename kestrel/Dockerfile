ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 tzdata ffmpeg

COPY run.sh /run.sh
COPY recorder.py /recorder.py
COPY morning.py /morning.py
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
