# CGA 메뉴얼

CGA 메뉴얼은 사용 목적에 따라 다음 세 문서로 제공합니다.

CGA-alone에서 달라진 봇 생성·2차 답변·공유방·클라이언트 사용은 [CGA-alone 개발 기능 사용 안내](CGA_ALONE_DEVELOPMENT_GUIDE.md)를 먼저 확인하십시오. 아래 원본 CGA 안내와 함께 읽으며, 배포 여부와 실제 외부 연동 확인 범위를 구분합니다.

## 어떤 문서를 읽을까요?

| 문서 | 대상 | 목적 |
|---|---|---|
| [CGA Getting Started](cga-getting-started/README.md) | 처음 사용하는 사용자 | 첫 봇 생성, 학습 상태 확인, 봇 테스트 흐름을 익힙니다. |
| [CGA 사용자 설명서](cga-user-manual/README.md) | 일반 사용자·봇 운영자·시스템 관리자 | 봇·버전·대화 설계·테스트·운영·관리자 메뉴의 전체 흐름을 확인합니다. |
| [CGA NLU 활용 가이드](cga-nlu-guide/README.md) | 봇·대화 설계 운영자·AI/NLU 담당자 | ML·Semantic·LLM 엔진 선택, 데이터 준비, 학습·인덱싱, 품질 개선 방법을 확인합니다. |

## 권장 읽기 순서

1. 처음 사용하는 경우 Getting Started에서 봇 생성부터 봇 테스트까지 따라 합니다.
2. 메뉴와 운영 절차가 필요하면 사용자 설명서를 확인합니다.
3. 엔진 선택이나 학습·검색·생성 품질을 다룰 때는 NLU 활용 가이드를 확인합니다.

## 지원 언어

각 문서는 한국어, 영어, 중국어(간체), 일본어, 베트남어, 프랑스어, 독일어로 제공합니다.

- Markdown 원본: 각 문서 폴더의 README, README.en, README.zh-CN, README.ja, README.vi, README.fr, README.de 파일
- Word 산출물: [dist](dist/) 폴더
- Web PDF: apps/web/public/manuals 폴더 및 /manuals/cga-{문서}-{언어}.pdf 경로
- 기존 한국어 링크는 호환성을 위해 언어 코드 없는 PDF 경로도 함께 제공합니다.

## 문서 운영 기준

- CGA 화면과 소스에서 사용하는 명칭을 우선합니다.
- 사용자는 DB나 CLI를 직접 조작하지 않고 화면의 상태·오류·대상 정보를 기준으로 조치합니다.
- 학습 요청은 Queue에 등록되고 Worker가 비동기로 처리하므로 학습 이력의 성공 또는 학습된 상태를 확인한 뒤 봇 테스트를 실행합니다.
- 브라우저 호출은 same-origin 경로를 사용하며 내부 서비스 주소는 화면이나 메뉴얼에 노출하지 않습니다.

## 다시 생성하는 방법

Word 문서는 build_manual_docx.py가 7개 언어의 세 문서를 생성합니다. 생성된 Word 문서는 LibreOffice로 PDF 변환한 뒤 apps/web/public/manuals에 언어별 이름으로 배치합니다. 배포 전에 문서 링크, PDF 헤더, 페이지 수, 첫 페이지 언어, 화면 레이아웃을 확인합니다.
