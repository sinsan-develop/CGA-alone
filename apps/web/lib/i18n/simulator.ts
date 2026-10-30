import type { SupportedLanguage } from "@/lib/language";

export type SimulatorCatalog = {
  botTestBreadcrumb: string; botTestTitle: string; loading: string; loadFailed: string; inputPlaceholder: string;
  send: string; analysisData: string; closeAnalysis: string; emptyAnalysis: string; openSimulator: string;
  closeSimulator: string; dialogLabel: string; runtimeTab: string; defaultGreeting: string;
  intentFallback: string; multiIntentGuide: string; runtimeFlowError: string; systemError: string;
};

const enUiLabels = {
  "봇":"Bot","HTML 메시지":"HTML message","표시할 항목이 없습니다.":"No items to display.","DTMF 입력":"DTMF input",
  "입력 대기 중":"Waiting for input","지우기":"Delete","입력 완료":"Submit input","대화 재시작":"Restart conversation",
  "플로팅 버튼 닫기":"Close floating buttons","대화 불러오기":"Load conversation","대화 저장하기":"Save conversation","대화 전체보기":"View full conversation",
  "사용자 발화":"User utterance","선택 의도":"Selected intent","상세 진단 데이터":"Detailed diagnostic data","의도분류 Score-in":"Intent classification Score-in",
  "의도분류 Score-out":"Intent classification Score-out","변수 스냅샷":"Variable snapshots","변수 스냅샷 없음":"No variable snapshots",
  "불러온 대화 전체보기":"View loaded conversation","확인":"Confirm","대상 없음":"No targets","전체 변수 스냅샷":"All variable snapshots",
  "전체 처리 로그":"All processing logs","처리 로그 없음":"No processing logs","현재 런타임 상태":"Current runtime state","대화":"Conversation",
  "다음 카드":"Next card","대기 카드":"Waiting card","종료 여부":"End status","세션 종료":"Session ended","대화 종료":"Conversation ended","진행 중":"In progress",
  "변수값 없음":"No variable values","(빈 값)":"(empty)","개체 인식":"Entity recognition","인식된 개체 없음":"No recognized entities","미인식":"Not recognized",
  "오류 / 이상 상황":"Errors / anomalies","오류 없음":"No errors","시나리오 흐름":"Scenario flow","카드 실행 후 변수값":"Variable values after card execution",
  "표시할 카드 흐름 없음":"No card flow to display","의도 인식 적용 단계":"Applied intent recognition stage","적용 대상":"Applied target",
  "의도":"Intent","스코어/확률":"Score/Probability","검증 문장 진단":"Validation utterance diagnostics","등록된 Validation 문장이 없습니다.":"No validation utterances are registered.",
  "Score Setting":"Score Setting","대화시작 카드":"Conversation start card","연결된 의도":"Connected intent","분류 결과":"Classification result",
  "Semantic 검색 근거":"Semantic search evidence","LLM 분류 근거":"LLM classification evidence","형태소":"Morphemes","근거":"Evidence","분류 토큰":"Classification tokens",
  "판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"Decision order: Exclude/Ignore → Small Talk → Exact Matching → Rule → ML/Semantic/LLM. Once a stage applies, later stages are not run.",
  "불러올 사용자 문장이 없습니다.":"There are no user utterances to load.","JSON 대화 파일을 읽지 못했습니다.":"Could not read the JSON conversation file.",
  "이미지":"Image","이미지 열기":"Open image","동영상":"Video","시간 선택":"Select time","토글":"Toggle","입력값":"Input","이전으로":"Back","항목":"Item","카카오맵":"Kakao Map","지도":"Map","스카이뷰":"Sky View","위치":"Location","선택":"Select","선택하세요":"Select","팝업 열기":"Open popup","닫기":"Close","팝업타이틀":"Popup title","팝업":"Popup","팝업에 표시할 데이터가 없습니다.":"No data to display in the popup."
} as const;

const richFormNativeLabels = {
  "zh-CN": { "이미지":"图片","이미지 열기":"打开图片","동영상":"视频","시간 선택":"选择时间","토글":"切换","입력값":"输入值","이전으로":"返回","항목":"项目","카카오맵":"Kakao 地图","지도":"地图","스카이뷰":"天空视图","위치":"位置","선택":"选择","선택하세요":"请选择","팝업 열기":"打开弹窗","닫기":"关闭","팝업타이틀":"弹窗标题","팝업":"弹窗","팝업에 표시할 데이터가 없습니다.":"弹窗中没有可显示的数据。" },
  ja: { "이미지":"画像","이미지 열기":"画像を開く","동영상":"動画","시간 선택":"時刻を選択","토글":"切り替え","입력값":"入力値","이전으로":"戻る","항목":"項目","카카오맵":"Kakaoマップ","지도":"地図","스카이뷰":"スカイビュー","위치":"場所","선택":"選択","선택하세요":"選択してください","팝업 열기":"ポップアップを開く","닫기":"閉じる","팝업타이틀":"ポップアップタイトル","팝업":"ポップアップ","팝업에 표시할 데이터가 없습니다.":"ポップアップに表示するデータがありません。" },
  vi: { "이미지":"Hình ảnh","이미지 열기":"Mở hình ảnh","동영상":"Video","시간 선택":"Chọn thời gian","토글":"Chuyển đổi","입력값":"Giá trị nhập","이전으로":"Quay lại","항목":"Mục","카카오맵":"Bản đồ Kakao","지도":"Bản đồ","스카이뷰":"Chế độ xem bầu trời","위치":"Vị trí","선택":"Chọn","선택하세요":"Vui lòng chọn","팝업 열기":"Mở cửa sổ bật lên","닫기":"Đóng","팝업타이틀":"Tiêu đề cửa sổ bật lên","팝업":"Cửa sổ bật lên","팝업에 표시할 데이터가 없습니다.":"Không có dữ liệu để hiển thị trong cửa sổ bật lên." },
  fr: { "이미지":"Image","이미지 열기":"Ouvrir l’image","동영상":"Vidéo","시간 선택":"Sélectionner l’heure","토글":"Basculer","입력값":"Valeur saisie","이전으로":"Retour","항목":"Élément","카카오맵":"Carte Kakao","지도":"Carte","스카이뷰":"Vue du ciel","위치":"Emplacement","선택":"Sélectionner","선택하세요":"Sélectionnez","팝업 열기":"Ouvrir la fenêtre","닫기":"Fermer","팝업타이틀":"Titre de la fenêtre","팝업":"Fenêtre","팝업에 표시할 데이터가 없습니다.":"Aucune donnée à afficher dans la fenêtre." },
  de: { "이미지":"Bild","이미지 열기":"Bild öffnen","동영상":"Video","시간 선택":"Uhrzeit auswählen","토글":"Umschalten","입력값":"Eingabewert","이전으로":"Zurück","항목":"Element","카카오맵":"Kakao-Karte","지도":"Karte","스카이뷰":"Himmelsansicht","위치":"Standort","선택":"Auswählen","선택하세요":"Bitte auswählen","팝업 열기":"Popup öffnen","닫기":"Schließen","팝업타이틀":"Popup-Titel","팝업":"Popup","팝업에 표시할 데이터가 없습니다.":"Keine Daten im Popup verfügbar." },
} as const satisfies Record<Exclude<SupportedLanguage, "ko" | "en">, Record<string, string>>;

type SimulatorUiLabelKey = keyof typeof enUiLabels;

const simulatorNativeLabels = {
  "zh-CN": {
    "봇":"机器人","HTML 메시지":"HTML 消息","표시할 항목이 없습니다.":"没有可显示的项目。","DTMF 입력":"DTMF 输入","입력 대기 중":"等待输入","지우기":"删除","입력 완료":"提交输入","대화 재시작":"重新开始对话","플로팅 버튼 닫기":"关闭浮动按钮","대화 불러오기":"加载对话","대화 저장하기":"保存对话","대화 전체보기":"查看完整对话","사용자 발화":"用户话语","선택 의도":"所选意图","상세 진단 데이터":"详细诊断数据","의도분류 Score-in":"意图分类 Score-in","의도분류 Score-out":"意图分类 Score-out","변수 스냅샷":"变量快照","변수 스냅샷 없음":"没有变量快照","불러온 대화 전체보기":"查看已加载的完整对话","확인":"确认","대상 없음":"无目标","전체 변수 스냅샷":"全部变量快照","전체 처리 로그":"全部处理日志","처리 로그 없음":"没有处理日志","현재 런타임 상태":"当前运行状态","대화":"对话","다음 카드":"下一张卡片","대기 카드":"等待卡片","종료 여부":"结束状态","세션 종료":"会话已结束","대화 종료":"对话已结束","진행 중":"进行中","변수값 없음":"没有变量值","(빈 값)":"（空值）","개체 인식":"实体识别","인식된 개체 없음":"未识别到实体","미인식":"未识别","오류 / 이상 상황":"错误 / 异常","오류 없음":"没有错误","시나리오 흐름":"场景流程","카드 실행 후 변수값":"卡片执行后的变量值","표시할 카드 흐름 없음":"没有可显示的卡片流程","의도 인식 적용 단계":"意图识别应用阶段","적용 대상":"应用目标","의도":"意图","스코어/확률":"分数/概率","검증 문장 진단":"验证语句诊断","등록된 Validation 문장이 없습니다.":"没有已登记的验证语句。","Score Setting":"分数设置","대화시작 카드":"对话开始卡片","연결된 의도":"已连接的意图","분류 결과":"分类结果","Semantic 검색 근거":"语义搜索依据","LLM 분류 근거":"LLM 分类依据","형태소":"词素","근거":"依据","분류 토큰":"分类令牌","판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"判定顺序：排除/忽略 → 闲聊 → 精确匹配 → 规则 → ML/语义/LLM。前一阶段生效后，不再执行后续阶段。","불러올 사용자 문장이 없습니다.":"没有可加载的用户语句。","JSON 대화 파일을 읽지 못했습니다.":"无法读取 JSON 对话文件。",
    ...richFormNativeLabels["zh-CN"],
  },
  ja: {
    "봇":"ボット","HTML 메시지":"HTMLメッセージ","표시할 항목이 없습니다.":"表示する項目がありません。","DTMF 입력":"DTMF入力","입력 대기 중":"入力待ち","지우기":"削除","입력 완료":"入力を確定","대화 재시작":"会話を再開","플로팅 버튼 닫기":"フローティングボタンを閉じる","대화 불러오기":"会話を読み込む","대화 저장하기":"会話を保存","대화 전체보기":"会話全体を表示","사용자 발화":"ユーザー発話","선택 의도":"選択インテント","상세 진단 데이터":"詳細診断データ","의도분류 Score-in":"インテント分類 Score-in","의도분류 Score-out":"インテント分類 Score-out","변수 스냅샷":"変数スナップショット","변수 스냅샷 없음":"変数スナップショットはありません","불러온 대화 전체보기":"読み込んだ会話全体を表示","확인":"確認","대상 없음":"対象なし","전체 변수 스냅샷":"すべての変数スナップショット","전체 처리 로그":"すべての処理ログ","처리 로그 없음":"処理ログはありません","현재 런타임 상태":"現在のランタイム状態","대화":"会話","다음 카드":"次のカード","대기 카드":"待機カード","종료 여부":"終了状態","세션 종료":"セッション終了","대화 종료":"会話終了","진행 중":"進行中","변수값 없음":"変数値はありません","(빈 값)":"（空）","개체 인식":"エンティティ認識","인식된 개체 없음":"認識されたエンティティはありません","미인식":"未認識","오류 / 이상 상황":"エラー / 異常","오류 없음":"エラーはありません","시나리오 흐름":"シナリオフロー","카드 실행 후 변수값":"カード実行後の変数値","표시할 카드 흐름 없음":"表示するカードフローはありません","의도 인식 적용 단계":"インテント認識の適用段階","적용 대상":"適用対象","의도":"インテント","스코어/확률":"スコア/確率","검증 문장 진단":"検証文の診断","등록된 Validation 문장이 없습니다.":"登録された検証文はありません。","Score Setting":"スコア設定","대화시작 카드":"会話開始カード","연결된 의도":"接続されたインテント","분류 결과":"分類結果","Semantic 검색 근거":"セマンティック検索の根拠","LLM 분류 근거":"LLM分類の根拠","형태소":"形態素","근거":"根拠","분류 토큰":"分類トークン","판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"判定順序：除外/無視 → スモールトーク → 完全一致 → ルール → ML/セマンティック/LLM。前の段階が適用されると、後続段階は実行されません。","불러올 사용자 문장이 없습니다.":"読み込むユーザー発話がありません。","JSON 대화 파일을 읽지 못했습니다.":"JSON会話ファイルを読み込めませんでした。",
    ...richFormNativeLabels.ja,
  },
  vi: {
    "봇":"Bot","HTML 메시지":"Tin nhắn HTML","표시할 항목이 없습니다.":"Không có mục nào để hiển thị.","DTMF 입력":"Nhập DTMF","입력 대기 중":"Đang chờ nhập","지우기":"Xóa","입력 완료":"Gửi dữ liệu nhập","대화 재시작":"Khởi động lại hội thoại","플로팅 버튼 닫기":"Đóng nút nổi","대화 불러오기":"Tải hội thoại","대화 저장하기":"Lưu hội thoại","대화 전체보기":"Xem toàn bộ hội thoại","사용자 발화":"Phát ngôn người dùng","선택 의도":"Ý định đã chọn","상세 진단 데이터":"Dữ liệu chẩn đoán chi tiết","의도분류 Score-in":"Phân loại ý định Score-in","의도분류 Score-out":"Phân loại ý định Score-out","변수 스냅샷":"Ảnh chụp biến","변수 스냅샷 없음":"Không có ảnh chụp biến","불러온 대화 전체보기":"Xem toàn bộ hội thoại đã tải","확인":"Xác nhận","대상 없음":"Không có đối tượng","전체 변수 스냅샷":"Tất cả ảnh chụp biến","전체 처리 로그":"Tất cả nhật ký xử lý","처리 로그 없음":"Không có nhật ký xử lý","현재 런타임 상태":"Trạng thái chạy hiện tại","대화":"Hội thoại","다음 카드":"Thẻ tiếp theo","대기 카드":"Thẻ chờ","종료 여부":"Trạng thái kết thúc","세션 종료":"Phiên đã kết thúc","대화 종료":"Hội thoại đã kết thúc","진행 중":"Đang thực hiện","변수값 없음":"Không có giá trị biến","(빈 값)":"(trống)","개체 인식":"Nhận dạng thực thể","인식된 개체 없음":"Không có thực thể được nhận dạng","미인식":"Không nhận dạng","오류 / 이상 상황":"Lỗi / bất thường","오류 없음":"Không có lỗi","시나리오 흐름":"Luồng kịch bản","카드 실행 후 변수값":"Giá trị biến sau khi chạy thẻ","표시할 카드 흐름 없음":"Không có luồng thẻ để hiển thị","의도 인식 적용 단계":"Giai đoạn áp dụng nhận dạng ý định","적용 대상":"Đối tượng áp dụng","의도":"Ý định","스코어/확률":"Điểm/Xác suất","검증 문장 진단":"Chẩn đoán câu xác thực","등록된 Validation 문장이 없습니다.":"Không có câu xác thực nào được đăng ký.","Score Setting":"Cài đặt điểm","대화시작 카드":"Thẻ bắt đầu hội thoại","연결된 의도":"Ý định được liên kết","분류 결과":"Kết quả phân loại","Semantic 검색 근거":"Căn cứ tìm kiếm ngữ nghĩa","LLM 분류 근거":"Căn cứ phân loại LLM","형태소":"Hình vị","근거":"Căn cứ","분류 토큰":"Mã thông báo phân loại","판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"Thứ tự đánh giá: Loại trừ/Bỏ qua → Trò chuyện ngắn → Khớp chính xác → Quy tắc → ML/Ngữ nghĩa/LLM. Khi một giai đoạn được áp dụng, các giai đoạn sau không chạy.","불러올 사용자 문장이 없습니다.":"Không có phát ngôn người dùng để tải.","JSON 대화 파일을 읽지 못했습니다.":"Không thể đọc tệp hội thoại JSON.",
    ...richFormNativeLabels.vi,
  },
  fr: {
    "봇":"Bot","HTML 메시지":"Message HTML","표시할 항목이 없습니다.":"Aucun élément à afficher.","DTMF 입력":"Saisie DTMF","입력 대기 중":"En attente de saisie","지우기":"Supprimer","입력 완료":"Valider la saisie","대화 재시작":"Redémarrer la conversation","플로팅 버튼 닫기":"Fermer les boutons flottants","대화 불러오기":"Charger la conversation","대화 저장하기":"Enregistrer la conversation","대화 전체보기":"Voir toute la conversation","사용자 발화":"Énoncé utilisateur","선택 의도":"Intention sélectionnée","상세 진단 데이터":"Données de diagnostic détaillées","의도분류 Score-in":"Classification d’intention Score-in","의도분류 Score-out":"Classification d’intention Score-out","변수 스냅샷":"Instantanés des variables","변수 스냅샷 없음":"Aucun instantané de variable","불러온 대화 전체보기":"Voir toute la conversation chargée","확인":"Confirmer","대상 없음":"Aucune cible","전체 변수 스냅샷":"Tous les instantanés des variables","전체 처리 로그":"Tous les journaux de traitement","처리 로그 없음":"Aucun journal de traitement","현재 런타임 상태":"État d’exécution actuel","대화":"Conversation","다음 카드":"Carte suivante","대기 카드":"Carte en attente","종료 여부":"État de fin","세션 종료":"Session terminée","대화 종료":"Conversation terminée","진행 중":"En cours","변수값 없음":"Aucune valeur de variable","(빈 값)":"(vide)","개체 인식":"Reconnaissance d’entités","인식된 개체 없음":"Aucune entité reconnue","미인식":"Non reconnu","오류 / 이상 상황":"Erreurs / anomalies","오류 없음":"Aucune erreur","시나리오 흐름":"Flux du scénario","카드 실행 후 변수값":"Valeurs des variables après l’exécution de la carte","표시할 카드 흐름 없음":"Aucun flux de carte à afficher","의도 인식 적용 단계":"Étape de reconnaissance d’intention appliquée","적용 대상":"Cible appliquée","의도":"Intention","스코어/확률":"Score/Probabilité","검증 문장 진단":"Diagnostic des phrases de validation","등록된 Validation 문장이 없습니다.":"Aucune phrase de validation n’est enregistrée.","Score Setting":"Paramètres du score","대화시작 카드":"Carte de début de conversation","연결된 의도":"Intention connectée","분류 결과":"Résultat de classification","Semantic 검색 근거":"Justification de la recherche sémantique","LLM 분류 근거":"Justification de la classification LLM","형태소":"Morphèmes","근거":"Justification","분류 토큰":"Jetons de classification","판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"Ordre de décision : Exclure/Ignorer → Conversation légère → Correspondance exacte → Règle → ML/Sémantique/LLM. Lorsqu’une étape s’applique, les suivantes ne sont pas exécutées.","불러올 사용자 문장이 없습니다.":"Aucun énoncé utilisateur à charger.","JSON 대화 파일을 읽지 못했습니다.":"Impossible de lire le fichier de conversation JSON.",
    ...richFormNativeLabels.fr,
  },
  de: {
    "봇":"Bot","HTML 메시지":"HTML-Nachricht","표시할 항목이 없습니다.":"Keine Elemente zum Anzeigen.","DTMF 입력":"DTMF-Eingabe","입력 대기 중":"Warten auf Eingabe","지우기":"Löschen","입력 완료":"Eingabe bestätigen","대화 재시작":"Gespräch neu starten","플로팅 버튼 닫기":"Schwebende Schaltflächen schließen","대화 불러오기":"Gespräch laden","대화 저장하기":"Gespräch speichern","대화 전체보기":"Gesamtes Gespräch anzeigen","사용자 발화":"Benutzeräußerung","선택 의도":"Ausgewählter Intent","상세 진단 데이터":"Detaillierte Diagnosedaten","의도분류 Score-in":"Intent-Klassifizierung Score-in","의도분류 Score-out":"Intent-Klassifizierung Score-out","변수 스냅샷":"Variablen-Snapshots","변수 스냅샷 없음":"Keine Variablen-Snapshots","불러온 대화 전체보기":"Gesamtes geladenes Gespräch anzeigen","확인":"Bestätigen","대상 없음":"Keine Ziele","전체 변수 스냅샷":"Alle Variablen-Snapshots","전체 처리 로그":"Alle Verarbeitungsprotokolle","처리 로그 없음":"Keine Verarbeitungsprotokolle","현재 런타임 상태":"Aktueller Laufzeitstatus","대화":"Gespräch","다음 카드":"Nächste Karte","대기 카드":"Wartende Karte","종료 여부":"Endstatus","세션 종료":"Sitzung beendet","대화 종료":"Gespräch beendet","진행 중":"In Bearbeitung","변수값 없음":"Keine Variablenwerte","(빈 값)":"(leer)","개체 인식":"Entitätserkennung","인식된 개체 없음":"Keine Entitäten erkannt","미인식":"Nicht erkannt","오류 / 이상 상황":"Fehler / Auffälligkeiten","오류 없음":"Keine Fehler","시나리오 흐름":"Szenarioablauf","카드 실행 후 변수값":"Variablenwerte nach Kartenausführung","표시할 카드 흐름 없음":"Kein Kartenablauf zum Anzeigen","의도 인식 적용 단계":"Angewandte Intent-Erkennungsstufe","적용 대상":"Anwendungsziel","의도":"Intent","스코어/확률":"Score/Wahrscheinlichkeit","검증 문장 진단":"Diagnose der Validierungssätze","등록된 Validation 문장이 없습니다.":"Es sind keine Validierungssätze registriert.","Score Setting":"Score-Einstellungen","대화시작 카드":"Gesprächsstartkarte","연결된 의도":"Verknüpfter Intent","분류 결과":"Klassifizierungsergebnis","Semantic 검색 근거":"Begründung der semantischen Suche","LLM 분류 근거":"Begründung der LLM-Klassifizierung","형태소":"Morpheme","근거":"Begründung","분류 토큰":"Klassifizierungstoken","판정 순서: 제외/무시 → 스몰토크 → Exacting Matching → 룰 → ML/시멘틱/LLM. 앞 단계가 적용되면 이후 단계는 실행하지 않습니다.":"Entscheidungsreihenfolge: Ausschließen/Ignorieren → Small Talk → Exakte Übereinstimmung → Regel → ML/Semantik/LLM. Sobald eine Stufe greift, werden spätere Stufen nicht ausgeführt.","불러올 사용자 문장이 없습니다.":"Es gibt keine Benutzeräußerungen zum Laden.","JSON 대화 파일을 읽지 못했습니다.":"Die JSON-Gesprächsdatei konnte nicht gelesen werden.",
    ...richFormNativeLabels.de,
  },
} satisfies Record<Exclude<SupportedLanguage, "ko" | "en">, Record<SimulatorUiLabelKey, string>>;

const simulatorUiLabels: Record<SupportedLanguage, Partial<Record<SimulatorUiLabelKey, string>>> = {
  ko: {},
  en: enUiLabels,
  "zh-CN": simulatorNativeLabels["zh-CN"],
  ja: simulatorNativeLabels.ja,
  vi: simulatorNativeLabels.vi,
  fr: simulatorNativeLabels.fr,
  de: simulatorNativeLabels.de,
};

const sharedRoomKoreanLabels = {
  "대화 방식": "대화 방식",
  "1:1 미리보기": "1:1 미리보기",
  "공유방": "공유방",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.",
  "참여자 이름": "참여자 이름",
  "방 ID": "방 ID",
  "공유방 ID": "공유방 ID",
  "새 공유방": "새 공유방",
  "참가자로 입장": "참가자로 입장",
  "현재 방:": "현재 방:",
  "답변 처리 중": "답변 처리 중",
  "내 후속 답변을 기다립니다.": "내 후속 답변을 기다립니다.",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.",
  "봇에게 질문할 수 있습니다.": "봇에게 질문할 수 있습니다.",
  "방 상태 확인 중": "방 상태 확인 중",
  "일반 대화": "일반 대화",
  "봇에게 질문": "봇에게 질문",
  "운영 버전 확인 중": "운영 버전 확인 중",
  "공유방 상태를 확인하지 못했습니다.": "공유방 상태를 확인하지 못했습니다.",
  "공유방 연결에 실패했습니다.": "공유방 연결에 실패했습니다.",
  "메시지 전송을 확인하지 못했습니다.": "메시지 전송을 확인하지 못했습니다.",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요."
} as const;
const sharedRoomUiLabels = {
  ko: sharedRoomKoreanLabels,
  "en": {
  "대화 방식": "Conversation mode",
  "1:1 미리보기": "1:1 preview",
  "공유방": "Shared room",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "Shared rooms use the operating version. Invite participants with the room ID.",
  "참여자 이름": "Participant name",
  "방 ID": "Room ID",
  "공유방 ID": "Shared room ID",
  "새 공유방": "New shared room",
  "참가자로 입장": "Join as participant",
  "현재 방:": "Current room:",
  "답변 처리 중": "Processing answer",
  "내 후속 답변을 기다립니다.": "Waiting for your follow-up answer.",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "Another participant is talking to the bot. General chat is available.",
  "봇에게 질문할 수 있습니다.": "You can ask the bot.",
  "방 상태 확인 중": "Checking room status",
  "일반 대화": "General chat",
  "봇에게 질문": "Ask bot",
  "운영 버전 확인 중": "Checking operating version",
  "공유방 상태를 확인하지 못했습니다.": "Could not verify shared room status.",
  "공유방 연결에 실패했습니다.": "Could not connect to the shared room.",
  "메시지 전송을 확인하지 못했습니다.": "Could not confirm message delivery.",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "The message will not be resent automatically. Check the history."
},
  "zh-CN": {
  "대화 방식": "对话模式",
  "1:1 미리보기": "一对一预览",
  "공유방": "共享聊天室",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "共享聊天室使用运行版本。可以用聊天室 ID 邀请参与者。",
  "참여자 이름": "参与者名称",
  "방 ID": "聊天室 ID",
  "공유방 ID": "共享聊天室 ID",
  "새 공유방": "新建共享聊天室",
  "참가자로 입장": "以参与者身份加入",
  "현재 방:": "当前聊天室：",
  "답변 처리 중": "正在处理回答",
  "내 후속 답변을 기다립니다.": "正在等待您的补充回答。",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "其他参与者正在与机器人对话。仍可发送普通消息。",
  "봇에게 질문할 수 있습니다.": "现在可以向机器人提问。",
  "방 상태 확인 중": "正在确认聊天室状态",
  "일반 대화": "普通消息",
  "봇에게 질문": "向机器人提问",
  "운영 버전 확인 중": "正在确认运行版本",
  "공유방 상태를 확인하지 못했습니다.": "无法确认共享聊天室状态。",
  "공유방 연결에 실패했습니다.": "无法连接共享聊天室。",
  "메시지 전송을 확인하지 못했습니다.": "无法确认消息是否送达。",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "不会自动重新发送。请查看记录。"
},
  "ja": {
  "대화 방식": "会話モード",
  "1:1 미리보기": "1対1プレビュー",
  "공유방": "共有ルーム",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "共有ルームは運用バージョンを使用します。ルームIDで参加者を招待できます。",
  "참여자 이름": "参加者名",
  "방 ID": "ルームID",
  "공유방 ID": "共有ルームID",
  "새 공유방": "新しい共有ルーム",
  "참가자로 입장": "参加者として入室",
  "현재 방:": "現在のルーム：",
  "답변 처리 중": "回答を処理中",
  "내 후속 답변을 기다립니다.": "あなたの追加回答を待っています。",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "他の参加者がボットと会話中です。通常のチャットは利用できます。",
  "봇에게 질문할 수 있습니다.": "ボットに質問できます。",
  "방 상태 확인 중": "ルームの状態を確認中",
  "일반 대화": "通常のチャット",
  "봇에게 질문": "ボットに質問",
  "운영 버전 확인 중": "運用バージョンを確認中",
  "공유방 상태를 확인하지 못했습니다.": "共有ルームの状態を確認できませんでした。",
  "공유방 연결에 실패했습니다.": "共有ルームに接続できませんでした。",
  "메시지 전송을 확인하지 못했습니다.": "メッセージの送信を確認できませんでした。",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "自動で再送しません。履歴を確認してください。"
},
  "vi": {
  "대화 방식": "Chế độ hội thoại",
  "1:1 미리보기": "Xem trước 1:1",
  "공유방": "Phòng chung",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "Phòng chung dùng phiên bản đang vận hành. Mời người tham gia bằng ID phòng.",
  "참여자 이름": "Tên người tham gia",
  "방 ID": "ID phòng",
  "공유방 ID": "ID phòng chung",
  "새 공유방": "Phòng chung mới",
  "참가자로 입장": "Tham gia phòng",
  "현재 방:": "Phòng hiện tại:",
  "답변 처리 중": "Đang xử lý trả lời",
  "내 후속 답변을 기다립니다.": "Đang chờ câu trả lời tiếp theo của bạn.",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "Người khác đang nói chuyện với bot. Bạn vẫn có thể trò chuyện thông thường.",
  "봇에게 질문할 수 있습니다.": "Bạn có thể hỏi bot.",
  "방 상태 확인 중": "Đang kiểm tra trạng thái phòng",
  "일반 대화": "Trò chuyện thường",
  "봇에게 질문": "Hỏi bot",
  "운영 버전 확인 중": "Đang kiểm tra phiên bản vận hành",
  "공유방 상태를 확인하지 못했습니다.": "Không thể xác nhận trạng thái phòng chung.",
  "공유방 연결에 실패했습니다.": "Không thể kết nối phòng chung.",
  "메시지 전송을 확인하지 못했습니다.": "Không thể xác nhận việc gửi tin nhắn.",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "Không tự động gửi lại. Vui lòng kiểm tra lịch sử."
},
  "fr": {
  "대화 방식": "Mode de conversation",
  "1:1 미리보기": "Aperçu individuel",
  "공유방": "Salon partagé",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "Les salons partagés utilisent la version en service. Invitez des participants avec l’identifiant du salon.",
  "참여자 이름": "Nom du participant",
  "방 ID": "ID du salon",
  "공유방 ID": "ID du salon partagé",
  "새 공유방": "Nouveau salon partagé",
  "참가자로 입장": "Rejoindre le salon",
  "현재 방:": "Salon actuel :",
  "답변 처리 중": "Réponse en cours",
  "내 후속 답변을 기다립니다.": "Votre réponse complémentaire est attendue.",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "Un autre participant parle au bot. La discussion générale reste disponible.",
  "봇에게 질문할 수 있습니다.": "Vous pouvez interroger le bot.",
  "방 상태 확인 중": "Vérification du salon",
  "일반 대화": "Discussion générale",
  "봇에게 질문": "Interroger le bot",
  "운영 버전 확인 중": "Vérification de la version en service",
  "공유방 상태를 확인하지 못했습니다.": "Impossible de vérifier le salon partagé.",
  "공유방 연결에 실패했습니다.": "Impossible de rejoindre le salon partagé.",
  "메시지 전송을 확인하지 못했습니다.": "Impossible de confirmer l’envoi du message.",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "Aucun renvoi automatique. Consultez l’historique."
},
  "de": {
  "대화 방식": "Gesprächsmodus",
  "1:1 미리보기": "Einzelvorschau",
  "공유방": "Gemeinsamer Raum",
  "공유방은 운영 버전으로 대화합니다. 방 ID로 다른 참여자를 초대할 수 있습니다.": "Gemeinsame Räume verwenden die aktive Version. Laden Sie Teilnehmer mit der Raum-ID ein.",
  "참여자 이름": "Teilnehmername",
  "방 ID": "Raum-ID",
  "공유방 ID": "ID des gemeinsamen Raums",
  "새 공유방": "Neuer gemeinsamer Raum",
  "참가자로 입장": "Als Teilnehmer beitreten",
  "현재 방:": "Aktueller Raum:",
  "답변 처리 중": "Antwort wird verarbeitet",
  "내 후속 답변을 기다립니다.": "Ihre ergänzende Antwort wird erwartet.",
  "다른 참여자가 봇과 대화 중입니다. 일반 대화는 가능합니다.": "Ein anderer Teilnehmer spricht mit dem Bot. Allgemeiner Chat ist weiterhin möglich.",
  "봇에게 질문할 수 있습니다.": "Sie können den Bot fragen.",
  "방 상태 확인 중": "Raumstatus wird geprüft",
  "일반 대화": "Allgemeiner Chat",
  "봇에게 질문": "Bot fragen",
  "운영 버전 확인 중": "Aktive Version wird geprüft",
  "공유방 상태를 확인하지 못했습니다.": "Der Raumstatus konnte nicht geprüft werden.",
  "공유방 연결에 실패했습니다.": "Verbindung zum gemeinsamen Raum fehlgeschlagen.",
  "메시지 전송을 확인하지 못했습니다.": "Die Nachrichtenzustellung konnte nicht bestätigt werden.",
  "자동으로 다시 보내지 않습니다. 기록을 확인해 주세요.": "Keine automatische Wiederholung. Bitte prüfen Sie den Verlauf."
}
} satisfies Record<SupportedLanguage, Record<keyof typeof sharedRoomKoreanLabels, string>>;

export function getSimulatorLabel(language: SupportedLanguage, value: string): string {
  const shared = sharedRoomUiLabels[language][value as keyof typeof sharedRoomKoreanLabels];
  if (shared !== undefined) return shared;
  const key = value as SimulatorUiLabelKey;
  return simulatorUiLabels[language][key] ?? (language === "ko" ? value : enUiLabels[key] ?? value);
}
export const SIMULATOR_CATALOGS = {
  ko: {
    botTestBreadcrumb: "봇 테스트 사용하기 > 봇과 대화하기", botTestTitle: "봇 테스트",
    loading: "봇 테스트 정보를 불러오는 중입니다.", loadFailed: "봇 정보를 불러오지 못했습니다.",
    inputPlaceholder: "챗봇과 대화를 진행해보세요.", send: "전송", analysisData: "분석 데이터",
    closeAnalysis: "분석 데이터 닫기", emptyAnalysis: "대화를 진행하면 분석 데이터가 표시됩니다.",
    openSimulator: "봇 테스트 열기", closeSimulator: "봇 테스트 닫기", dialogLabel: "봇 테스트",
    runtimeTab: "Runtime / Variables / Trace", defaultGreeting: "{bot} 대화 시뮬레이터입니다. 저장된 대화 기준으로 테스트할 문장을 입력하세요.",
    intentFallback: "질문을 이해하지 못했습니다. 다시 말씀해주세요.", multiIntentGuide: "아래 후보 중 원하는 의도를 선택해주세요.",
    runtimeFlowError: "대화 흐름 설정 오류로 대화를 계속할 수 없습니다.", systemError: "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
  },
  en: {
    botTestBreadcrumb: "Use Bot Test > Talk to the Bot", botTestTitle: "Bot Test",
    loading: "Loading bot test information.", loadFailed: "Failed to load bot information.",
    inputPlaceholder: "Start a conversation with the bot.", send: "Send", analysisData: "Analysis Data",
    closeAnalysis: "Close analysis data", emptyAnalysis: "Analysis data appears after you start a conversation.",
    openSimulator: "Open Bot Test", closeSimulator: "Close Bot Test", dialogLabel: "Bot Test",
    runtimeTab: "Runtime / Variables / Trace", defaultGreeting: "This is the {bot} conversation simulator. Enter a sentence to test the saved conversation.",
    intentFallback: "I could not understand your question. Please try again.", multiIntentGuide: "Please select the intent you want below.",
    runtimeFlowError: "The conversation cannot continue because of a flow configuration error.", systemError: "An error occurred while processing. Please try again later.",
  },
  "zh-CN": {
    botTestBreadcrumb: "使用机器人测试 > 与机器人对话", botTestTitle: "机器人测试",
    loading: "正在加载机器人测试信息。", loadFailed: "无法加载机器人信息。",
    inputPlaceholder: "开始与机器人对话。", send: "发送", analysisData: "分析数据",
    closeAnalysis: "关闭分析数据", emptyAnalysis: "开始对话后将显示分析数据。",
    openSimulator: "打开机器人测试", closeSimulator: "关闭机器人测试", dialogLabel: "机器人测试",
    runtimeTab: "运行时 / 变量 / 跟踪", defaultGreeting: "这是 {bot} 对话模拟器。请输入要基于已保存对话测试的句子。",
    intentFallback: "未能理解您的问题，请重新说明。", multiIntentGuide: "请从以下候选中选择您需要的意图。",
    runtimeFlowError: "由于对话流程配置错误，无法继续对话。", systemError: "处理过程中发生错误，请稍后重试。",
  },
  ja: {
    botTestBreadcrumb: "ボットテストを使う > ボットと会話する", botTestTitle: "ボットテスト",
    loading: "ボットテスト情報を読み込み中です。", loadFailed: "ボット情報を読み込めませんでした。",
    inputPlaceholder: "ボットとの会話を始めてください。", send: "送信", analysisData: "分析データ",
    closeAnalysis: "分析データを閉じる", emptyAnalysis: "会話を開始すると分析データが表示されます。",
    openSimulator: "ボットテストを開く", closeSimulator: "ボットテストを閉じる", dialogLabel: "ボットテスト",
    runtimeTab: "ランタイム / 変数 / トレース", defaultGreeting: "{bot} 会話シミュレーターです。保存済み会話をテストする文を入力してください。",
    intentFallback: "質問を理解できませんでした。もう一度お話しください。", multiIntentGuide: "以下の候補から希望するインテントを選択してください。",
    runtimeFlowError: "会話フローの設定エラーにより会話を続行できません。", systemError: "処理中にエラーが発生しました。しばらくしてから再試行してください。",
  },
  vi: {
    botTestBreadcrumb: "Sử dụng kiểm thử bot > Trò chuyện với bot", botTestTitle: "Kiểm thử bot",
    loading: "Đang tải thông tin kiểm thử bot.", loadFailed: "Không thể tải thông tin bot.",
    inputPlaceholder: "Bắt đầu trò chuyện với bot.", send: "Gửi", analysisData: "Dữ liệu phân tích",
    closeAnalysis: "Đóng dữ liệu phân tích", emptyAnalysis: "Dữ liệu phân tích sẽ xuất hiện sau khi bắt đầu hội thoại.",
    openSimulator: "Mở kiểm thử bot", closeSimulator: "Đóng kiểm thử bot", dialogLabel: "Kiểm thử bot",
    runtimeTab: "Thời gian chạy / Biến / Truy vết", defaultGreeting: "Đây là trình mô phỏng hội thoại {bot}. Nhập câu để kiểm thử hội thoại đã lưu.",
    intentFallback: "Tôi chưa hiểu câu hỏi. Vui lòng nói lại.", multiIntentGuide: "Vui lòng chọn ý định mong muốn bên dưới.",
    runtimeFlowError: "Không thể tiếp tục do lỗi cấu hình luồng hội thoại.", systemError: "Đã xảy ra lỗi khi xử lý. Vui lòng thử lại sau.",
  },
  fr: {
    botTestBreadcrumb: "Utiliser le test du bot > Parler au bot", botTestTitle: "Test du bot",
    loading: "Chargement des informations du test.", loadFailed: "Impossible de charger les informations du bot.",
    inputPlaceholder: "Commencez une conversation avec le bot.", send: "Envoyer", analysisData: "Données d’analyse",
    closeAnalysis: "Fermer les données d’analyse", emptyAnalysis: "Les données d’analyse apparaîtront après le début de la conversation.",
    openSimulator: "Ouvrir le test du bot", closeSimulator: "Fermer le test du bot", dialogLabel: "Test du bot",
    runtimeTab: "Exécution / Variables / Trace", defaultGreeting: "Voici le simulateur de conversation {bot}. Saisissez une phrase pour tester la conversation enregistrée.",
    intentFallback: "Je n’ai pas compris votre question. Veuillez reformuler.", multiIntentGuide: "Veuillez sélectionner l’intention souhaitée ci-dessous.",
    runtimeFlowError: "La conversation ne peut pas continuer en raison d’une erreur de configuration du flux.", systemError: "Une erreur s’est produite pendant le traitement. Veuillez réessayer plus tard.",
  },
  de: {
    botTestBreadcrumb: "Bot-Test verwenden > Mit dem Bot sprechen", botTestTitle: "Bot-Test",
    loading: "Bot-Testinformationen werden geladen.", loadFailed: "Bot-Informationen konnten nicht geladen werden.",
    inputPlaceholder: "Starten Sie ein Gespräch mit dem Bot.", send: "Senden", analysisData: "Analysedaten",
    closeAnalysis: "Analysedaten schließen", emptyAnalysis: "Analysedaten werden nach Beginn des Gesprächs angezeigt.",
    openSimulator: "Bot-Test öffnen", closeSimulator: "Bot-Test schließen", dialogLabel: "Bot-Test",
    runtimeTab: "Laufzeit / Variablen / Trace", defaultGreeting: "Dies ist der Gesprächssimulator für {bot}. Geben Sie einen Testsatz für das gespeicherte Gespräch ein.",
    intentFallback: "Ich konnte Ihre Frage nicht verstehen. Bitte formulieren Sie sie neu.", multiIntentGuide: "Bitte wählen Sie unten den gewünschten Intent aus.",
    runtimeFlowError: "Das Gespräch kann wegen eines Konfigurationsfehlers nicht fortgesetzt werden.", systemError: "Bei der Verarbeitung ist ein Fehler aufgetreten. Bitte versuchen Sie es später erneut.",
  },
} satisfies Record<SupportedLanguage, SimulatorCatalog>;

export const SIMULATOR_DTMF_CATALOGS = {
  ko: { guide: "{min}~{max}자리 입력 후 {end}을 누르세요. 최초 {first}초, 전체 {overall}초", lengthError: "{min}~{max}자리로 입력하세요." },
  en: { guide: "Enter {min} to {max} digits, then press {end}. First input: {first}s, total: {overall}s", lengthError: "Enter {min} to {max} digits." },
  "zh-CN": { guide: "输入 {min}~{max} 位后按 {end}。首次 {first} 秒，总计 {overall} 秒", lengthError: "请输入 {min}~{max} 位。" },
  ja: { guide: "{min}〜{max}桁を入力してから {end} を押してください。初回 {first}秒、全体 {overall}秒", lengthError: "{min}〜{max}桁で入力してください。" },
  vi: { guide: "Nhập {min}~{max} chữ số rồi nhấn {end}. Lần đầu {first} giây, tổng {overall} giây", lengthError: "Hãy nhập {min}~{max} chữ số." },
  fr: { guide: "Saisissez {min} à {max} chiffres, puis appuyez sur {end}. Première saisie : {first}s, total : {overall}s", lengthError: "Saisissez {min} à {max} chiffres." },
  de: { guide: "Geben Sie {min} bis {max} Ziffern ein und drücken Sie dann {end}. Erste Eingabe: {first}s, gesamt: {overall}s", lengthError: "Geben Sie {min} bis {max} Ziffern ein." },
} satisfies Record<SupportedLanguage, { guide: string; lengthError: string }>;
export function formatSimulatorText(template: string, values: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => values[key] ?? "");
}
