# 채널별 연결 대상 관리

## 적용 범위

시스템 연계 → 채널 관리에서 기존 채널 종류 아래 여러 연결 대상을 등록한다. 같은 Slack의 서로 다른 워크스페이스·방 등을 따로 저장할 수 있다. 연결 대상은 답변봇을 새로 생성하지 않는다.

설정 등록·조회·편집·삭제·설정 검사는 외부 접속 없이 사용할 수 있다. 연결 버튼은 서버 실행 설정과 해당 Provider 실행부가 준비된 경우에만 활성화된다. 저장이나 설정 검사 성공을 실제 메신저 연결 성공으로 판단하지 않는다.

## 원본 CGA 채널 아이디 유지

봇스테이션과 대화 실행은 원본 CGA의 표준 채널 코드를 사용한다. 새 메신저 채널을 생성할 때는 아래 아이디를 사용하고, 여러 계정·워크스페이스·방은 그 채널 아래 [연결 대상 추가]로 등록한다. 채널 이름은 구분하기 쉽게 정할 수 있다. 기존 Webchat·카카오톡·Teams 채널은 그대로 사용한다.

| 종류 | 채널 아이디 |
|---|---|
| Webchat | WEBCHAT |
| 카카오톡 | KAKAO |
| Teams | TEAMS |
| Slack | SLACK |
| Discord | DISCORD |
| Telegram | TELEGRAM |
| Google Chat | GOOGLE_CHAT |
| LINE | LINE |

예를 들어 Slack은 채널 아이디 `SLACK`, Provider `Slack`으로 만들고, 봇스테이션의 해당 채널·운영 버전을 준비한다. 임의의 다른 아이디로 설정을 저장한 것만으로 대화 실행까지 확인된 것은 아니다. 이는 원본 CGA 실행 규칙을 유지하는 조건이며, 연결 대상 이름이나 외부 계정 하나에 고정한다는 뜻이 아니다.

## 등록

1. 기존 채널을 선택하고 [연결 대상]을 연다. 채널 종류가 없으면 [채널 생성]에서 해당 Provider를 등록한다. 기존 Webchat/카카오 설정은 유지한다.
2. [연결 대상 추가]에서 표시 이름, 답변봇, 설치·워크스페이스 ID, 외부 봇 계정 ID, 대상 방·채널 ID를 입력한다. 이 ID는 설정값이며 외부 인증 전에는 검증된 신원으로 쓰지 않는다.
3. 필요한 인증정보를 보호 입력란에 넣고 [설정 저장]을 누른다. 실제 토큰을 대화창·CSV·기존 인증 JSON에 붙여 넣지 않는다.
4. 목록에는 인증정보 등록 여부만 표시된다. 서버 암호화 키가 미설정이면 인증정보 저장은 거부된다. 실제 키·토큰 등록은 관리자 환경 준비·승인 후 한다.
5. 같은 외부 봇 계정·설치·대상은 중복 등록할 수 없다. 다른 방은 별도 연결로 등록한다.

## 수정·검사·삭제

- [수정] 시 인증정보는 공란이다. 공란은 기존 값을 유지하며, 특정 필드만 입력하면 해당 필드만 교체한다. 마스킹 문자열을 입력하지 않는다.
- [설정 검사]는 설정/보호 저장 상태를 검사한다. ‘실제 접속 미확인’ 표시를 확인한다. 외부 계정 인증이나 질문·모델 호출은 실행하지 않는다.
- 연결 중 설정 변경·삭제는 먼저 해제해야 한다. 다른 연결 대상에는 영향을 주지 않는다.
- [삭제]는 확인 후 해당 설정을 숨긴다. 대화 기록·외부 앱·토큰은 자동 삭제/폐기하지 않는다.
- 살아있는 연결 설정이 있는 부모 채널은 삭제하거나 Provider/사용 여부를 바꿀 수 없다. 자식 연결을 정리한 뒤 수행한다. 채널 표시 이름 수정은 가능하다.

## 후속 실제 연계 준비

Webchat/카카오톡은 기존 CGA 연결을 유지한다. Teams는 개인 Microsoft 로그인과 봇 앱 등록 조건을 구분한다. 공식 개발 절차는 custom app upload가 허용된 M365 테넌트와 공개 HTTPS 수신 주소를 요구한다. 개인 로그인만으로 봇 설치 가능을 확인한 것은 아니다.

Slack → Discord → Telegram → Google Chat → LINE 순으로 실행부와 연결·해제·재연결 설명을 추가한다. 실제 계정/Secret/앱 설치·공개 주소·게시·배포는 별도 승인으로 진행한다.

Teams 준비 근거: https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/get-started/quickstart-register

## 공통 실행 상태(C02)

실행은 서버의 CHANNEL_CONNECTION_WORKER_ENABLED 기본 false이다. 승인된 실제 Provider 실행부와 암호화 키가 준비된 경우에만 연결 버튼이 활성화된다. 연결 명령은 연결 중으로 표시하며, 실제 인증과 수신 준비 확인 뒤에만 연결됨으로 바뀐다. 화면은 연결 희망 대상의 상태를 자동 갱신한다.

동일 앱의 수신 연결을 공유해도 대상별 해제는 독립적이다. 해제 후 과거 질문을 자동으로 가져오지 않으며, 게시 결과가 불명확하면 자동으로 재전송하지 않는다. 서비스 재시작은 연결 희망 상태를 복원하되 명시적으로 해제한 대상은 유지한다. C02만으로 실제 메신저 접속이 제공되는 것은 아니며 각 Provider 구현·실제 인증 검증은 후속 단계다.

## Teams 연결(C03)

### 준비 조건

현재 구현은 Microsoft Bot Connector의 단일 테넌트 앱을 대상으로 한다. 개인 Microsoft 계정 로그인만으로 테넌트·봇 앱·사용자 지정 앱 설치 권한이 준비된 것은 아니다. 관리자가 Teams 앱 설치를 허용한 조직 환경과 Azure Bot 앱 등록, 공개 HTTPS 수신 주소가 필요하다. 실제 앱·키 등록과 공개 주소 설정은 별도 승인 후 진행한다.

### 등록 순서

1. 조직의 Microsoft Entra/Azure Bot 설정에서 테넌트 UUID와 앱(client) UUID를 확인한다. 봇의 Teams 채널을 준비하고 앱의 bot ID를 해당 앱 UUID로 맞춘다. 필요한 권한·구독·비용은 실제 등록 전에 확인한다.
2. 승인된 공개 HTTPS 호스트의 `/api/v1/channel-connections/ms-teams/events`를 봇의 Messaging endpoint로 등록한다. WSL의 내부 HTTP 주소를 외부 수신 주소로 사용하지 않는다.
3. CGA-alone의 시스템 연계 → 채널 관리에서 기존 MS Teams 채널을 선택하고 [연결 대상] → [연결 대상 추가]를 연다. 답변할 봇은 기존 운영 버전과 해당 채널의 봇스테이션 설정이 준비되어 있어야 한다.
4. 설치·워크스페이스 ID에는 테넌트 UUID, 외부 봇 계정 ID에는 앱 UUID, 대상 방·채널 ID에는 Teams 채널 ID 또는 개인/그룹 채팅의 conversation ID를 넣는다. 채널은 channelData.channel.id와, 개인/그룹은 conversation.id와 대조한다. 표시 이름이나 사용자 이메일을 ID 대신 넣지 않는다.
5. 보호 입력란 `tenant_id`, `client_id`, `client_secret`에 해당 앱의 값을 등록한다. 앞의 두 값은 연결 ID 설정과 일치해야 한다. Secret은 문서·채팅·CSV·기존 인증 JSON에 기록하지 않는다. 서버의 연결 암호화 키가 없으면 등록할 수 없다.
6. [설정 검사]는 저장된 설정 검사이다. [연결]을 누르면 서버가 Microsoft 인증을 확인하고 **첫 인증된 대상 메시지를 기다리는 동안 ‘연결 중’**을 유지한다. 해당 Teams 대상에서 봇을 호출한다. 서버가 서명·앱·테넌트·대상을 모두 확인한 뒤에만 ‘연결됨’으로 바뀐다.
7. 고정 Q/A 질문으로 답변을 확인하고, 같은 앱의 다른 대상은 별도 연결로 등록한다. A 해제 후 B가 유지되는지 확인한다. 다른 CGA 조직에서 같은 앱을 동시에 수신하도록 등록하면 거부한다.

서버 관리자는 승인 후 API의 `CHANNEL_CONNECTION_WORKER_ENABLED`와 `CHANNEL_CONNECTION_ENCRYPTION_KEY`, `CHANNEL_CONNECTION_KEY_VERSION`을 설정한다. Compose는 기본 false/빈 키로 실행한다. 이 매뉴얼 작성이나 설정 저장만으로 키 등록·실행 활성화를 하지 않는다.

### 대화와 카드

개인 채팅은 봇에 보낸 메시지를 질문으로 처리한다. 그룹·채널에서는 봇을 명시적으로 호출한 메시지와 해당 봇 카드의 선택만 실행한다. 채널의 스레드는 Teams conversation ID에 따라 분리하고, 같은 방의 답변 처리 중 새 질문을 나중에 실행하도록 대기열에 쌓지 않는다. 일반 대화는 봇 답변을 만들지 않는다.

기존 text/button/link-button/card/table/carousel과 Teams용 Adaptive Card 템플릿을 재사용한다. 카드의 원본 메시지·노드와 질문자를 서버에서 대조한다. Action.Submit의 입력·data와 수동 Action.Execute를 기존 응답 변수로 전달하며 원래 입력 이름을 보존한다. 내부 필드와 같은 입력명도 사용할 수 있으며, 자동 카드 refresh는 질문·모델 실행을 일으키지 않는다. Webchat 전용 양식을 Teams 카드로 자동 변환하는 기능은 제공하지 않으므로 해당 채널의 템플릿을 작성한다.

질문은 기존 API의 4,000자 제한을 따른다. 수신 HTTP 본문은 256 KiB, 생성 표현은 24 KB, 실제 회신 문맥을 포함한 패킷은 28 KB 이내로 제한한다. 형식·크기를 전송 전에 검사한다. 여러 패킷 전송 중 시간초과·프로세스 종료가 발생해 결과가 불명확하면 자동 재전송하지 않는다. 재접속 때 과거 질문을 자동으로 재실행하지 않는다.

### 해제·재연결과 오류

- [연결 해제]는 해당 대상만 중단한다. 기존 대화 기록과 외부 앱은 유지된다. 설정 변경·삭제는 해제 후 수행한다.
- [연결]로 다시 시작하며, 기존 해제 대상은 서버 재시작만으로 자동 연결되지 않는다. 연결 희망 상태였던 대상은 저장된 검증 정보와 현재 인증을 다시 확인한다.
- ‘첫 메시지 대기’: 대상 ID와 앱 설치를 확인하고 그 대상에서 봇을 호출한다. 단순 저장·OAuth 성공은 수신 검증이 아니다.
- ‘앱·테넌트·인증정보 확인’: UUID 일치, Secret 만료, 운영 봇·채널 설정을 관리자와 확인한다. 비밀값을 오류 보고에 붙이지 않는다.
- ‘다른 CGA 조직에서 사용 중’: 기존 조직의 해당 앱 연결을 먼저 해제하거나 별도 앱을 사용한다.
- ‘수신·Microsoft 연결 확인’: 승인된 공개 endpoint, 네트워크와 서비스 상태를 확인한다. 결과 불명 전송을 임의 재실행하지 않는다.

### 검증 범위

C03 개발 검증은 합성 JWT, PostgreSQL 원장, 기존 고정 Q/A·카드 실행과 대역 Microsoft 전송을 사용한다. 실제 Teams 테넌트·앱 설치·Microsoft 왕복은 **NOT_RUN / 인수 필수**다. 외부 HTTPS·키·실제 호출 승인 후 이 절차를 실제 환경에서 확인해야 한다.

근거: [Bot Connector 인증](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication?view=azure-bot-service-4.0), [Teams 카드](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference), [Universal Actions](https://learn.microsoft.com/en-us/adaptive-cards/authoring-cards/universal-action-model).


## Slack 연결(C04)

### 준비

기존 워크스페이스에 설치할 Slack 앱을 준비한다. 이 단계는 **Socket Mode**로 수신하므로 외부에 공개한 HTTP 수신 주소가 필요하지 않다. 앱 설치·토큰 발급·서버 키 등록과 실제 연결 활성화는 별도 승인 후 진행한다.

1. Slack 앱의 Socket Mode를 켜고 앱 수준 토큰을 준비한다. 앱 수준 토큰에는 `connections:write` 권한을 설정한다. 보호 입력란 `app_token`에 보관할 값이다.
2. Bot Token Scopes에 `chat:write`, `users:read`, `app_mentions:read`와 연결할 대화 종류의 조회 권한을 준비한다. 공개 채널은 `channels:read`, 비공개 채널은 `groups:read`, 개인 대화는 `im:read`, 다인 대화는 `mpim:read`를 사용한다. 메시지 구독에는 종류별 `channels:history`, `groups:history`, `im:history`, `mpim:history`가 필요할 수 있다. 사용하는 종류에 맞춰 최소 권한을 설정하고 앱을 설치한다.
3. Event Subscriptions에서 `app_mention`, 개인 대화용 `message.im`을 설정한다. 일반 채팅이나 그룹 메시지도 수신하려면 해당 `message.channels`, `message.groups`, `message.mpim` 이벤트와 권한을 설정한다. Interactivity도 켠다. 사용하지 않는 명령·모달은 등록하지 않는다.
4. 봇을 대상 채널에 초대한다. 이 실행부는 채널 자동 참가·생성이나 권한 변경을 하지 않는다.

### CGA-alone 등록

시스템 연계 → 채널 관리에서 Slack Provider 채널을 만들거나 기존 채널을 선택하고 [연결 대상] → [연결 대상 추가]를 연다.

| 입력 | Slack 값 |
|---|---|
| 연결 이름 | 관리자가 구분할 이름 |
| 답변 봇 | 기존 운영 버전·봇스테이션이 준비된 본인 봇 |
| 설치·워크스페이스 ID | `auth.test`의 `team_id` (`T...`) |
| 외부 봇 계정 ID | `auth.test`의 `user_id` (`U...` 또는 `W...`). `bot_id` (`B...`)와 구분한다. |
| 대상 방·채널 ID | Slack의 채널·DM ID (`C...`, `G...`, `D...`) |
| bot_token | 설치된 앱의 Bot User OAuth Token |
| app_token | 같은 Slack 앱의 앱 수준 Socket Mode 토큰 |

실제 토큰은 보호 입력란에만 넣는다. 채팅·문서·CSV·기존 채널 인증 JSON에 붙이지 않는다. 화면에서는 등록 여부만 표시하고 수정 시 공란은 기존 값을 유지한다. 같은 앱을 여러 대상에 연결할 때는 해당 앱의 같은 앱 수준 토큰을 사용한다. 다른 워크스페이스에는 각각 설치된 봇 토큰을 등록한다.

[설정 검사]는 저장 상태 검사이며 외부 인증 성공을 의미하지 않는다. [연결]은 서버 실행 설정과 암호화 키가 승인·준비된 경우에 사용할 수 있다. 서버는 workspace·bot user·bot·app·대상 채널을 조회하고, 실제 Socket의 hello에서 앱 ID를 대조한 뒤에만 연결됨을 표시한다. 같은 앱은 수신 연결 하나를 공유하며, 다른 CGA 조직에서 같은 앱을 중복 수신하지 않는다.

### 사용·해제·재연결

- 개인 대화는 봇에 보낸 메시지를 질문으로 처리한다. 공유 채널은 봇을 명시적으로 호출한 메시지와 해당 봇 카드의 선택만 실행한다. 일반 대화는 봇 답변을 만들지 않는다.
- 같은 채널이라도 Slack 스레드별로 기존 CGA 방을 분리한다. 다른 스레드의 질문자·문맥을 섞지 않는다. 같은 방의 답변 처리 중 들어온 새 질문을 나중에 실행하도록 쌓지 않는다.
- 기존 6종 표현(text/button/link-button/card/table/carousel)과 채널 템플릿을 사용한다. 카드 응답은 원본 메시지·노드·질문자를 서버에서 검증한다. 지원하지 않는 모달·슬래시 명령·편집 이벤트는 시나리오로 실행하지 않는다.
- A 연결을 [연결 해제]해도 같은 앱의 B 대상은 계속 사용할 수 있다. 설정 수정·삭제는 해제 후 한다. 삭제는 외부 앱 제거·토큰 폐기와 별개다.
- [연결]로 다시 시작할 수 있다. 서버 재시작 때 연결 희망 상태만 복구하며 명시적으로 해제한 대상은 유지한다. 과거 질문을 자동으로 가져오지 않는다.
- 수신 ACK는 질문을 PostgreSQL 원장에 저장한 뒤 보낸다. Slack 재수신은 기존 메시지 ID로 중복 실행을 방지한다. 게시 시간초과나 재시작으로 결과가 불명확하면 자동 재게시하지 않는다. 일반적인 수신 소켓 갱신은 답변 재전송과 구분한다.

### 문제 확인·검증 범위

권한·인증 오류는 앱 설치, 토큰의 workspace/app 일치와 만료, 채널 참가 여부를 확인한다. 다른 앱의 app_token을 섞거나 다른 CGA 조직에 같은 앱을 동시에 연결하지 않는다. 수신 문제는 Socket Mode·앱 이벤트·Interactivity 구독을 확인한다. Secret 원문을 오류 보고에 넣지 않는다.

개발 검증은 대역 Socket/Slack API, 실제 QA PostgreSQL과 기존 고정 Q/A·카드 실행을 사용한다. 실제 Slack 앱 설치·토큰 등록·Socket 접속·워크스페이스 왕복은 **NOT_RUN / 인수 필수**이며 별도 승인 후 확인한다.

근거: [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/), [auth.test](https://docs.slack.dev/reference/methods/auth.test/), [bots.info](https://docs.slack.dev/reference/methods/bots.info/), [conversations.info](https://docs.slack.dev/reference/methods/conversations.info/), [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/).

## Discord 연결(C05)

### 준비·등록

Discord Developer Portal에서 Bot이 있는 앱을 준비하고 대상 서버에 설치한다. 실제 앱 설치·토큰 등록·서버 실행 활성화는 별도 승인 후 진행한다. 사용자 계정 토큰이나 self-bot은 지원하지 않는다. 수신은 Gateway이고 답변은 Discord REST API에 게시하므로 별도 공개 webhook 주소는 필요하지 않다.

시스템 연계 → 채널 관리에서 채널 아이디 `DISCORD`, Provider `Discord`를 선택하고 [연결 대상 추가]를 연다. 기존 CGA의 운영 버전·봇스테이션 채널 설정을 먼저 준비한다.

| 입력 | Discord 값 |
|---|---|
| 연결 이름 | 서버·채널을 구분할 관리용 이름 |
| 답변 봇 | 연결할 CGA-alone 봇 |
| 설치·워크스페이스 ID | 서버(guild) ID. 기존 개인 DM은 `DM` |
| 외부 봇 계정 ID | Discord Bot User ID. Application ID와 구분 |
| 대상 방·채널 ID | 텍스트 채널, 공지 채널, 스레드 또는 기존 개인 DM의 ID |
| bot_token | 해당 Discord 앱의 Bot Token. 보호 입력란에만 등록 |

같은 앱을 여러 대상에 연결하려면 같은 앱의 Bot Token을 사용하고 대상별 연결을 추가한다. 다른 CGA 조직이 같은 앱의 Gateway를 중복 소유하지 못한다. 서버는 봇·앱·서버·대상·권한을 대조하고 Gateway의 READY 또는 인증된 저장 세션의 RESUMED를 확인한 뒤 연결됨으로 표시한다. [설정 검사]만으로 실제 연결이 확인되지는 않는다.

### 권한·질문 방식

- 기본 수신 intent는 GUILDS·GUILD_MESSAGES·DIRECT_MESSAGES이다. MESSAGE_CONTENT 등 특권 intent를 자동으로 켜지 않는다. 공유 채널에서는 봇을 명시적으로 멘션한 질문을 사용한다. 개인 DM은 봇에게 보낸 메시지를 사용한다.
- 텍스트 채널은 View Channel·Send Messages, 스레드는 부모 채널의 View Channel·Send Messages in Threads 권한이 필요하다. 카드·표 등 embed 표현에는 Embed Links도 필요하다. 비공개 스레드는 봇이 이미 참가한 경우만 연결한다. 채널 생성·자동 참가·권한 부여는 하지 않는다.
- 서버 텍스트/공지 채널, 접근 가능한 공개·비공개 스레드, 기존 봇 개인 DM을 지원한다. 다인 DM·음성·포럼 부모 자체·슬래시 명령·모달은 이번 실행 범위가 아니다. 여러 shard가 필요한 앱은 연결 성공으로 처리하지 않는다.
- 스레드와 채널은 각각 별도 CGA 방으로 구분한다. 일반 대화는 봇 답변을 만들지 않으며, 같은 방에서 답변 처리 중 들어온 새 질문을 나중에 실행하도록 쌓지 않는다. 다른 봇이 명시적으로 호출한 질문도 기존 질문자 구분으로 처리한다.
- 기존 6종 표현을 사용하고 버튼 선택은 원본 메시지·현재 카드·질문자를 서버에서 검증한다. 버튼 이벤트를 PostgreSQL 원장에 먼저 저장한 뒤 화면 메시지를 만들지 않는 접수 callback을 보낸다. 접수가 확인되기 전에는 시나리오·NLU·LLM을 실행하지 않는다.

### 해제·재연결·장애 확인

A 대상의 [연결 해제]는 같은 앱의 B 대상에 영향을 주지 않는다. 수정·삭제는 해제 후 한다. 모든 대상의 명시적 해제는 Gateway 재개 정보를 폐기한다. 재연결은 새 접수 기준 이후의 입력만 처리한다. 삭제는 Discord 앱 제거·토큰 폐기와 별개다.

서버 재시작·일시 소켓 장애는 PostgreSQL에 암호화한 세션과 마지막 영속 sequence로 재개한다. 인증된 동일 세대의 재개 이벤트만 원장에 반영하며 별도 과거 메시지 조회는 하지 않는다. 저장된 native event ID로 중복 실행·게시를 방지한다. Discord가 세션을 무효화하면 새 인증으로 연결하며 기존 CGA 대화 기록은 삭제하지 않는다.

버튼 접수나 답변 게시가 시간초과되어 결과가 불명확하면 자동으로 재시도하지 않는다. 접수 결과 불명인 질문은 실행하지 않는다. Bot Token·앱/봇 ID·서버/채널 ID·접근 권한·스레드 참가/잠금·세션 시작 제한을 확인하며 Secret 원문을 오류 보고에 넣지 않는다.

개발 검증은 대역 Gateway/REST, 실제 QA PostgreSQL, 기존 고정 Q/A·카드와 SDK를 사용한다. 실제 Discord 접속·앱 설치·토큰 등록·메시지 왕복은 **NOT_RUN / 인수 필수**이다.

근거: [Gateway](https://docs.discord.com/developers/events/gateway), [Gateway Events](https://docs.discord.com/developers/events/gateway-events), [Interactions](https://docs.discord.com/developers/interactions/receiving-and-responding), [Permissions](https://docs.discord.com/developers/topics/permissions).

재개가 불가능한 종료(예: 4007/4009) 또는 재개 버퍼 상한(64개·1 MiB)에 도달하면 같은 cursor로 재시도하지 않는다. 해당 Gateway cursor만 폐기하고 새 Identify/READY 이후의 입력을 받는다. 아직 원장에 접수하지 못한 구간은 기존 감사 이력의 `system.connection.discord_resume_reset`에 마지막 영속 sequence·관측된 끝·끝 범위 미확정 여부·고정 사유·시각을 남긴다. 기존 방·메시지·원장과 결과불명 요청은 보존한다. 이 구간의 질문은 자동으로 소급 실행하지 않으므로 필요한 질문은 새로 보내야 한다. 명시적 해제/재연결과 수신 장애 복구는 게시 재시도 승인이 아니다.

세션 종료 코드 근거: [Gateway 종료 코드](https://docs.discord.com/developers/topics/opcodes-and-status-codes).

## Telegram — C06


1. 시스템 연계→채널 관리에서 채널 ID TELEGRAM·Provider Telegram을 사용한다. 기존 운영 버전·봇스테이션을 준비한다.
2. 연결 이름과 CGA 답변봇을 선택한다. 설치·워크스페이스 ID와 외부 봇 계정 ID에는 getMe의 Bot User ID를 같은 값으로 넣고, 대상에는 숫자 chat ID(그룹은 음수 가능)를 넣는다. username은 getMe에서 확인하므로 별도 입력하지 않는다.
3. Bot Token은 보호 입력란에만 넣는다. 실제 토큰·서버 암호화 키·앱 등록·실제 활성화는 별도 승인이다. 설정 검사만으로 외부 접속이 확인된 것은 아니다.
4. 기존 webhook이나 다른 polling 프로그램이 있으면 CGA가 자동 변경하지 않는다. 기존 운영자를 확인해 별도로 정리해야 한다. 공개 HTTPS 수신 주소는 이 long-polling 경로에서 필요하지 않다.
5. 개인 대화는 봇에게 질문한다. 그룹 Privacy Mode에서는 /ask@봇username 질문처럼 해당 봇을 명시한 명령을 사용한다. 일반 @멘션은 Telegram이 전달하는 경우에만 처리한다. 자동 Privacy Mode/권한 변경은 하지 않는다.
6. 그룹·supergroup·topic 및 개인 방을 분리한다. 미디어 질문·익명 채널 발신·채널 게시를 개인 질문자로 합치지 않는다. 다른 봇/LLM은 기존 CGA 질문 API도 사용할 수 있으며 native에서는 플랫폼이 실제로 전달한 식별 가능한 입력만 다룬다.
7. 해제/수정/삭제는 대상별 독립적이다. 명시적 해제·재연결 뒤 구카드는 실행하지 않으므로 새 질문/시작 시나리오로 카드를 다시 받아야 한다. 자동 재시작은 기존 카드·방·offset을 유지한다. 게시/접수 결과불명은 자동 재시도하지 않는다.

근거: [Telegram Bot API](https://core.telegram.org/bots/api), [Privacy Mode](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).


7일 이상 새 수신이 없으면 높은 이전 offset을 전송하기 전에 offset=0으로 확인하고 새 수신 구간 번호를 보호 저장한다. 구간 안에서는 원장 commit 후에만 offset을 전진시킨다. 빈 조회 뒤 재시작해도 초기화가 반복되지 않으며 과거와 같은 update ID는 구간 번호로 구별한다. 기존 결과불명 요청·방·기록은 보존하고 자동 재실행하지 않는다. 플랫폼 보관기한(최대24시간)을 넘겨 삭제된 질문은 복구할 수 없다. 감사 이력 system.connection.telegram_idle_reset에서 고정 사유·시각·구간 전환을 확인한다.

실제 Telegram 인증·송수신은 **NOT_RUN / 인수 필수**이며 개발 검증은 합성 API·QA PostgreSQL·기존 고정 Q/A만 사용한다. [Update](https://core.telegram.org/bots/api#update), [Getting updates](https://core.telegram.org/bots/api#getting-updates).
