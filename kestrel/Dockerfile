ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 tzdata

COPY run.sh /run.sh
COPY recorder.py /recorder.py
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
