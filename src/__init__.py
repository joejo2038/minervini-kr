"""
패키지 초기화.

여기서 SSL 인증서 경로를 먼저 잡습니다.

맥용 python.org 설치본은 시스템 키체인을 쓰지 않고 자체 번들을 참조하는데,
설치 직후에는 그 번들이 비어 있습니다. 그 상태로 KRX에 붙으면
CERTIFICATE_VERIFY_FAILED가 납니다. certifi 경로를 환경변수로 넣어주면
urllib과 requests 양쪽 모두 정상 동작합니다.

ssl 모듈이 기본 컨텍스트를 만들기 전에 실행되어야 하므로
데이터 모듈보다 먼저 임포트되는 이 위치에 둡니다.
"""
from __future__ import annotations

import os


def _bootstrap_ssl() -> None:
    try:
        import certifi
    except ImportError:
        return

    bundle = certifi.where()
    if not os.path.exists(bundle):
        return

    # 사용자가 이미 지정했다면 존중합니다. 사내 프록시 환경 대응.
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    os.environ.setdefault("CURL_CA_BUNDLE", bundle)


_bootstrap_ssl()
