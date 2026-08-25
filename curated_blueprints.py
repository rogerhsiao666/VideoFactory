"""Curated, outcome-distinct blueprints for topics that need editorial control."""

from __future__ import annotations


PHONE_CONTRACT = {
    "audience": "一接到英語電話就腦袋空白、怕聽錯又不敢打斷的台灣成人",
    "core_pain": "看不到表情與文字時，無法即時聽懂、回應、記錄與修復通話",
    "promised_transformation": "能用一句明確原話控制通話節奏、確認資訊並完成下一步",
    "in_scope": [
        "接聽與撥出時立即開口",
        "處理語速、聲音、斷線與同時說話",
        "確認身份、目的、數字、日期、地址與下一步",
        "轉接、等待、留言、回撥與結束通話",
    ],
    "out_of_scope": [
        "購物退貨、保險或會議本身的業務知識",
        "深呼吸、準備講稿或心理建設",
        "只替換姓名日期地址的同句練習",
    ],
    "required_moments": [
        "電話響起或需要主動撥出",
        "聽不清、跟不上或腦袋空白",
        "需要記錄並核對高風險資訊",
        "斷線、轉接、等待、留言與回撥",
        "確認下一步並自然結束",
    ],
    "pain_categories": [
        "開口壓力", "身份與來意不明", "理解失速", "資訊記錄風險",
        "通訊失控", "轉接留言摩擦", "回應與收尾壓力",
    ],
}


PHONE_SPECS = [
    ("開口壓力", "接起陌生電話時報上身份", "learner_line", "Hello, this is Mei speaking.", "Hello, this is Mei speaking. How can I help?", "陌生號碼接通後，對方正等著我先開口", "沉默太久會讓對方以為電話沒有接通"),
    ("開口壓力", "主動撥出時直接交代來意", "learner_line", "I'm calling about my appointment.", "Hi, I'm calling about tomorrow's appointment with Dr. Lee.", "主動撥出後，不知道第一句如何切入正題", "只說 hello 會讓接聽者無法判斷該如何協助"),
    ("身份與來意不明", "陌生來電先確認對方身份", "learner_line", "May I ask who's calling?", "Sorry, may I ask who's calling before we continue?", "對方直接開始說事，卻沒有自我介紹", "未確認身份可能把個人資訊交給錯的人"),
    ("身份與來意不明", "轉接前聽懂對方詢問來意", "counterpart_line", "What is this regarding?", "Before I transfer you, what is this regarding?", "接線員在轉接前要求簡短說明來意", "聽不懂問題會再次卡住或被轉錯部門"),
    ("開口壓力", "意外接到電話時先爭取切換時間", "learner_line", "Give me a moment to switch gears.", "I wasn't expecting the call. Give me a moment to switch gears.", "突然接到英語電話，腦中還沒進入英文狀態", "倉促回答容易答非所問或答應錯誤事項"),
    ("資訊記錄風險", "對方要報資料前先拿紙筆", "learner_line", "Let me grab a pen.", "Let me grab a pen before you give me the number.", "對方準備快速報出號碼或地址", "沒有先記錄會被迫反覆追問或抄錯"),
    ("理解失速", "做筆記跟不上時要求降速", "learner_line", "Could you slow down a little?", "I'm taking notes. Could you slow down a little?", "對方連續說明，手寫速度已經跟不上", "漏掉中間步驟會導致後續執行錯誤"),
    ("理解失速", "只重聽斷掉的最後一段", "learner_line", "Could you repeat the last part?", "The line cut out. Could you repeat the last part?", "前面已聽懂，只有最後一段因訊號漏掉", "籠統要求全部重說會浪費時間又增加壓力"),
    ("身份與來意不明", "聽懂對方要求提供完整姓名", "counterpart_line", "May I have your full name?", "Before we continue, may I have your full name, please?", "接線員在查詢前要求我提供完整姓名", "沒聽懂就會沉默，或只報名字而無法完成身份核對"),
    ("資訊記錄風險", "請對方拼出陌生姓氏", "learner_line", "Could you spell your last name?", "Could you spell your last name so I record it correctly?", "姓名發音陌生，無法確定拼法", "拼錯姓名會找不到紀錄或寄錯後續資料"),
    ("資訊記錄風險", "聽懂對方主動澄清相近數字", "counterpart_line", "That's fifteen, not fifty.", "Just to clarify, that's fifteen, not fifty.", "對方發現 15 與 50 容易混淆，主動更正讀法", "漏聽 not 就會把正確數字記成十倍差異"),
    ("資訊記錄風險", "要求長號碼分組報讀", "learner_line", "Could you group the numbers?", "Could you give the number in groups of three?", "對方一次快速念完整串電話或案件編號", "工作記憶裝不下整串數字，容易漏碼或倒置"),
    ("資訊記錄風險", "回讀號碼讓對方立即校正", "learner_line", "Let me read that back.", "Let me read that back: zero nine, three one five.", "已經記下號碼，但不確定是否正確", "不當場回讀會把錯誤帶到付款、聯絡或預約流程"),
    ("資訊記錄風險", "確認星期與日期是否配對", "learner_line", "Did you say Thursday the thirteenth?", "Just checking, did you say Thursday the thirteenth?", "對方同時說了星期與日期，可能聽錯其中一個", "日期錯一天就可能錯過預約或截止時間"),
    ("資訊記錄風險", "聽懂時間包含指定時區", "counterpart_line", "That's three o'clock Eastern Time.", "The appointment is at three o'clock Eastern Time.", "跨地區通話中，對方在時間後補充 Eastern Time", "漏掉時區會在錯誤時間加入或回撥"),
    ("資訊記錄風險", "請對方改用文字傳送地址", "learner_line", "Could you text me the address?", "To avoid a mistake, could you text me the address?", "地址太長或包含陌生地名，口頭難以可靠記錄", "地址少一個字就可能導航到錯誤地點"),
    ("理解失速", "聽到艱深說法時要求白話重述", "learner_line", "Could you say that in plain English?", "I know the words, but could you say that in plain English?", "每個字似乎聽到，整句意思仍無法組起來", "假裝理解會在錯誤前提下做決定"),
    ("理解失速", "聽見內容但不懂意義時要求改述", "learner_line", "Could you rephrase that?", "I heard you, but I didn't understand. Could you rephrase that?", "重複原句仍然無法理解對方的意思", "只要求再說一次會再次卡在同一種說法"),
    ("通訊失控", "訊號斷續時明確指出問題", "learner_line", "You're breaking up.", "You're breaking up. Could you move somewhere with better reception?", "對方聲音每隔幾秒就消失", "把訊號問題誤認為英文不好會一直猜內容"),
    ("通訊失控", "回音嚴重時要求調整設備", "learner_line", "There's a strong echo.", "There's a strong echo. Could you turn off speakerphone?", "通話中自己的聲音不斷回傳", "回音會蓋住對方後半句並造成雙方搶話"),
    ("通訊失控", "背景噪音蓋過人聲時說明狀況", "learner_line", "I can barely hear you.", "There's loud background noise, and I can barely hear you.", "對方所在環境的噪音比人聲更大", "勉強猜測可能漏掉關鍵限制或指示"),
    ("通訊失控", "口頭通道失效時切換到電子郵件", "learner_line", "Could you email the details?", "The line is unclear. Could you email the details instead?", "音質反覆變差，繼續口頭確認沒有把握", "沒有文字備份就無法精確核對後續事項"),
    ("通訊失控", "重新接通後定位中斷位置", "learner_line", "Sorry, we got disconnected.", "Sorry, we got disconnected while you were explaining the next step.", "電話斷線後重新接通", "若不指出中斷位置，雙方不知道該從哪裡繼續"),
    ("通訊失控", "所在環境不適合時約定具體回撥時間", "learner_line", "May I call back in ten minutes?", "I'm somewhere noisy. May I call back in ten minutes?", "接電話時人在吵雜或無法談話的地方", "硬撐著談會漏聽，也可能讓旁人聽到敏感資訊"),
    ("通訊失控", "聽不清時請對方稍後回撥", "learner_line", "Could you call me back after three?", "Reception is poor here. Could you call me back after three?", "目前訊號不穩，但稍後會移動到較好地點", "沒有約定時間的回撥容易再次錯過彼此"),
    ("轉接留言摩擦", "聽懂對方要求暫時等待", "counterpart_line", "Can I put you on hold?", "Can I put you on hold while I check that?", "客服要查資料，先詢問能否讓我在線等待", "沒聽懂就可能突然沉默或誤以為斷線"),
    ("轉接留言摩擦", "可以等待時簡短答覆", "learner_line", "Yes, I'll hold.", "Yes, I'll hold. Please take your time.", "對方詢問是否願意在線等候", "冗長回應會打斷客服準備查詢的節奏"),
    ("轉接留言摩擦", "無法久候時改約回撥", "learner_line", "Could I call back instead?", "I can't stay on hold. Could I call back instead?", "當下沒有時間繼續在線等待", "直接掛掉會失去案件脈絡，也不知道何時再聯絡"),
    ("轉接留言摩擦", "聽懂即將轉接並保持在線", "counterpart_line", "I'll transfer you now.", "I'll transfer you now. Please stay on the line.", "接線員準備把電話轉給另一個部門", "未聽懂 stay on the line 可能提早掛斷"),
    ("轉接留言摩擦", "發現找錯部門時請求正確轉接", "learner_line", "Could you connect me with billing?", "I reached the wrong team. Could you connect me with billing?", "接電話的人無法處理，且我知道需要哪個部門", "重新撥總機會再次經歷相同焦慮與等待"),
    ("轉接留言摩擦", "被部門互踢時阻止重複轉回", "learner_line", "Please don't send me back to sales.", "I've already spoken to sales. Please don't transfer me back.", "不同部門互相轉接，問題沒有任何人接手", "再次被轉回會陷入無限循環並遺失說明內容"),
    ("轉接留言摩擦", "聽懂對方提供可直接回撥的分機", "counterpart_line", "My extension is four-two-seven.", "If we get disconnected, my extension is four-two-seven.", "即將轉接前，對方主動報出可直接回撥的分機", "漏掉分機後若斷線，只能重新從總機開始"),
    ("轉接留言摩擦", "聯絡人不在時主動要求留言", "learner_line", "Could I leave a message?", "She's unavailable, so could I leave a message?", "要找的人無法接電話", "只說稍後再打可能反覆錯過，也沒有留下來意"),
    ("轉接留言摩擦", "聽懂對方提供留言選項", "counterpart_line", "Would you like to leave a message?", "She's away from her desk. Would you like to leave a message?", "接線員說明聯絡人不在並提出下一步", "沒聽懂選項會錯過留下資訊的機會"),
    ("轉接留言摩擦", "留言時清楚交代回電目的", "learner_line", "Please ask her to call me.", "Please ask her to call me about tomorrow's appointment.", "需要讓不在場的聯絡人知道為何回電", "只留姓名沒有主旨，對方可能無法排定優先順序"),
    ("轉接留言摩擦", "留言時提供可聯絡時段與號碼", "learner_line", "She can reach me after two.", "She can reach me at 9123 4567 after two.", "對方需要知道何時、用哪個號碼回電", "漏掉時段或號碼會造成下一次未接"),
    ("開口壓力", "看到未接來電時自然回撥", "learner_line", "I'm returning a missed call.", "Hi, I'm returning a missed call from this number.", "手機顯示陌生未接來電，不知道對方是誰", "直接問 who are you 容易顯得突兀，也交代不清回撥原因"),
    ("開口壓力", "依照語音留言找到指定聯絡人", "learner_line", "I'm calling for David Chen.", "Hi, I'm calling for David Chen about his voicemail.", "語音留言要求回撥給某位聯絡人", "未提留言來源時，接線者不知道如何銜接"),
    ("回應與收尾壓力", "當下答不出時承諾查證後回覆", "learner_line", "I don't have that information yet.", "I don't have that information yet. Can I confirm and call back?", "對方突然詢問我手邊沒有的資訊", "隨口猜答案會造成後續承諾或資料錯誤"),
    ("回應與收尾壓力", "連續問題太多時控制一次一題", "learner_line", "One question at a time, please.", "I'm taking notes. One question at a time, please.", "對方一口氣問了好幾個問題", "同時處理多題會漏答、答錯順序或完全腦袋空白"),
    ("回應與收尾壓力", "雙方同時說話時把話權讓回去", "learner_line", "Sorry, you go ahead.", "We started speaking together. Sorry, you go ahead.", "雙方同時開口，彼此都停下來", "不處理搶話會反覆撞句並增加尷尬"),
    ("回應與收尾壓力", "聽懂背景後追問自己要做的動作", "learner_line", "What do you need me to do?", "I understand the issue. What do you need me to do next?", "對方說明很多背景，卻沒有清楚交代我的任務", "只懂背景但漏掉行動會造成事情停滯"),
    ("資訊記錄風險", "模糊的急件要求追問確切期限", "learner_line", "When exactly do you need this?", "To make sure I understood, when exactly do you need this?", "對方只說 soon、later 或 as soon as possible", "雙方對急迫程度理解不同會導致延誤"),
    ("回應與收尾壓力", "結束前用一句話回顧共識", "learner_line", "Let me make sure I understood.", "Let me make sure I understood: you'll email the form today.", "內容談完但我不確定雙方是否有相同結論", "沒有口頭摘要會把誤解帶到通話之後"),
    ("回應與收尾壓力", "掛電話前確認下一個流程", "learner_line", "What happens after this call?", "Before we finish, what happens after this call?", "問題似乎處理完，但沒人說明後續會發生什麼", "掛掉後不知道該等、該寄資料或該再次聯絡"),
    ("回應與收尾壓力", "聽懂對方可能因缺資料再次聯絡", "counterpart_line", "We'll call if we need anything else.", "We'll call tomorrow if we need anything else from you.", "對方說明案件後續可能再次來電", "沒聽懂就可能忽略下一通電話或誤以為已完全結案"),
    ("回應與收尾壓力", "聽懂客服最後確認是否仍需協助", "counterpart_line", "Is there anything else I can help with?", "Before we end, is there anything else I can help with?", "客服準備結束通話前做最後確認", "沒聽懂會錯過補問最後一個重要問題"),
    ("回應與收尾壓力", "問題已解決時明確表示可以結束", "learner_line", "That answers everything, thank you.", "That answers everything. Thank you for walking me through it.", "所有問題已回答，但不知道如何自然收尾", "只突然說 bye 可能顯得倉促，也未確認事情完成"),
    ("回應與收尾壓力", "時間到了時禮貌中止並約續談", "learner_line", "I need to go now.", "I'm sorry, I need to go now. Can we continue later?", "通話超過可用時間，但對方仍在繼續說", "硬撐會影響下一個安排，直接掛掉又顯得失禮"),
    ("回應與收尾壓力", "以等待書面確認作為最後共識", "learner_line", "I'll wait for your email.", "Thanks, I'll wait for your confirmation email before taking action.", "對方承諾寄送書面資料，通話準備結束", "沒有說清等待事項，可能太早行動或忘記追蹤"),
]


COMPLAINT_CONTRACT = {
    "audience": "遇到服務或商品出錯時怕太兇、又怕被敷衍的台灣成人",
    "core_pain": "無法把已發生的問題、實際影響、證據與合理補救說清楚",
    "promised_transformation": "能禮貌但堅定地指出問題、要求補救並留下可追蹤紀錄",
    "in_scope": [
        "指出具體錯誤並描述影響",
        "提出證據與明確補救要求",
        "聽懂政策推託、調查要求與替代方案",
        "拒絕不合理方案、升級主管與書面追蹤",
    ],
    "out_of_scope": [
        "尚未發生問題的詢價、折扣或設備詢問",
        "拒絕加班、借錢或一般人際要求",
        "只換商品場所但溝通目的完全相同的句子",
    ],
    "required_moments": [
        "第一次指出已發生的錯誤",
        "說明具體損失並出示證據",
        "提出可執行的補救與期限",
        "聽懂推託後堅持立場",
        "要求主管、案件編號與書面確認",
    ],
    "pain_categories": [
        "指出具體問題", "說明實際影響", "提出可核對證據", "要求明確補救",
        "聽懂推託與條件", "拒絕不合理方案", "升級與追蹤",
    ],
}


COMPLAINT_SPECS = [
    ("指出具體問題", "用低衝突開場指出房間有問題", "learner_line", "There's an issue with my room.", "I'm afraid there's a problem with the room I was given.", "入住後立刻發現房間狀況不符合預期", "只說不滿意太籠統，對方無法開始處理"),
    ("指出具體問題", "餐點上錯時直接指出不符", "learner_line", "This isn't what I ordered.", "This isn't what I ordered; I asked for the vegetarian option.", "送來的餐點和剛才點的內容不同", "不說明原本選項，服務生可能只道歉卻不換餐"),
    ("指出具體問題", "外送內容短少時指出缺件", "learner_line", "One item is missing.", "One item is missing from the order I received.", "拆開外送後發現明細上的品項沒有送到", "籠統說訂單有問題會拖慢核對與補送"),
    ("指出具體問題", "帳單重複扣款時明確指出", "learner_line", "I was charged twice.", "My card was charged twice for the same purchase.", "銀行通知顯示同一筆消費扣款兩次", "若只說金額不對，客服可能查不到重複交易"),
    ("指出具體問題", "新品很快故障時交代使用時間", "learner_line", "It stopped working after two days.", "It stopped working after only two days of normal use.", "正常使用兩天後商品完全無法運作", "未交代時間與正常使用，店家可能歸因於耗損或誤用"),
    ("指出具體問題", "入住後發現房間尚未清潔", "learner_line", "The room hasn't been cleaned.", "The room hasn't been cleaned since the previous guest left.", "進房後看到床單、垃圾與浴室都未整理", "只說房間不舒服不足以證明是清潔疏失"),
    ("指出具體問題", "包裹送達時已有明顯損壞", "learner_line", "My delivery arrived damaged.", "My delivery arrived damaged, and the box was already crushed.", "收件時外箱凹陷，內容物也破損", "未說明到貨即損壞，責任可能被推給收件後使用"),
    ("指出具體問題", "同一問題反覆發生時指出歷史", "learner_line", "I've reported this before.", "I've reported this twice, but the same issue keeps happening.", "相同故障已通報過，卻再次發生", "被當成新案件會從最初步驟重來且沒有改善責任"),
    ("說明實際影響", "延誤造成錯過轉乘", "learner_line", "I missed my connection.", "The delay meant I missed my connecting flight to Singapore.", "第一段航班延誤導致下一段已起飛", "只抱怨等待太久，對方看不到需要改票的實際損失"),
    ("說明實際影響", "服務中斷導致無法工作", "learner_line", "I couldn't work all morning.", "The outage meant I couldn't work all morning.", "網路或系統從早上起無法使用", "沒有說明工作受阻，案件可能被排為低優先級"),
    ("說明實際影響", "錯誤食材帶來過敏風險", "learner_line", "This could trigger an allergic reaction.", "The dish contains nuts, which could trigger an allergic reaction.", "已告知過敏仍收到含堅果的餐點", "只說不想吃會被誤認為偏好而非安全問題"),
    ("說明實際影響", "延誤造成額外住宿成本", "learner_line", "The delay cost me an extra night.", "The delay cost me an extra night at the hotel.", "交通延誤迫使我多住一晚", "若不說明額外支出，就沒有清楚的賠償依據"),
    ("提出可核對證據", "用收據證明實際扣款金額", "learner_line", "Here's the receipt.", "Here's the receipt showing the amount I was charged.", "店員表示系統金額與我的說法不同", "口頭爭辯沒有共同可核對的交易紀錄"),
    ("提出可核對證據", "用照片證明到貨損壞", "learner_line", "I have photos of the damage.", "I took photos before opening the damaged package.", "客服要求證明損壞是在開箱前就存在", "沒有時間點清楚的照片可能被認為是使用後造成"),
    ("提出可核對證據", "用預訂確認證明原始價格", "learner_line", "My confirmation shows a different price.", "My booking confirmation shows a lower price than this bill.", "結帳價格高於預訂郵件上的金額", "若不出示原始確認，對方可能只依現場價格收費"),
    ("提出可核對證據", "物流顯示已送達但實際未收到", "learner_line", "The tracking says it was delivered.", "The tracking says delivered, but nothing arrived at my address.", "系統標記送達，門口與管理室都沒有包裹", "只說沒收到可能被系統狀態直接駁回"),
    ("要求明確補救", "要求當天更換故障商品", "learner_line", "Could you replace it today?", "The item is defective. Could you replace it today?", "商品急需使用，不能等待長期送修", "未提出期限，店家可能只提供模糊的後續處理"),
    ("要求明確補救", "要求移除重複扣款", "learner_line", "Please remove the duplicate charge.", "Please remove the duplicate charge and confirm the refund date.", "帳單已確認存在兩筆相同交易", "只要求查看不會產生實際退款動作或時間"),
    ("要求明確補救", "商品無法使用時要求全額退款", "learner_line", "I'd like a full refund.", "The product is unusable, so I'd like a full refund.", "商品核心功能完全失效且不適合維修", "只說想退貨可能得到店內點數而非原付款退款"),
    ("要求明確補救", "錯過航班時要求改到下一班", "learner_line", "Could you rebook me on the next flight?", "I missed my connection. Could you rebook me on the next flight?", "延誤已造成原本轉機無法搭乘", "若只問怎麼辦，櫃檯不一定立即保留下一班座位"),
    ("要求明確補救", "房間未清潔時要求立即派人", "learner_line", "Please send someone to clean the room.", "The bathroom is dirty. Please send someone to clean the room.", "房間衛生問題可以現場立即補救", "沒有具體要求，飯店可能只記錄抱怨而不派人"),
    ("要求明確補救", "缺件時要求當天補送", "learner_line", "Can you deliver the missing item today?", "One item is missing. Can you deliver it today?", "外送訂單短少且今天就需要使用", "只要求退款無法解決當下缺少該物品的需求"),
    ("要求明確補救", "故障影響生活時提出維修期限", "learner_line", "Could you repair it by Friday?", "The heater is broken. Could you repair it by Friday?", "設備故障且接下來幾天就必須使用", "沒有期限的維修承諾可能被無限延後"),
    ("要求明確補救", "票券姓名錯誤時要求更正", "learner_line", "Please correct the name on the booking.", "The surname is misspelled. Please correct it before check-in.", "預訂姓名與證件拼法不同", "到現場才處理可能無法登機或入住"),
    ("要求明確補救", "非自願取消時要求免除手續費", "learner_line", "I'd like the cancellation fee waived.", "You canceled the service, so I'd like the fee waived.", "取消原因來自業者而非消費者", "系統仍可能自動扣除一般取消費"),
    ("聽懂推託與條件", "聽懂客服要求出示收據", "counterpart_line", "Could you show me the receipt?", "Could you show me the receipt so I can verify the charge?", "客服需要交易證據才能查帳", "沒聽懂要求會一直重述問題卻無法進入處理流程"),
    ("聽懂推託與條件", "聽懂客服要求損壞照片", "counterpart_line", "Can you send photos of the damage?", "Can you send us photos showing the damage clearly?", "客服把照片列為理賠或退換貨條件", "漏交指定證據會讓案件停在待補件狀態"),
    ("聽懂推託與條件", "聽懂店家引用不退款政策", "counterpart_line", "Our policy doesn't allow refunds.", "I'm sorry, but our policy doesn't allow refunds after thirty days.", "客服用公司政策拒絕退款", "若沒聽出這是拒絕理由，就不知道要針對例外或責任回應"),
    ("聽懂推託與條件", "聽懂店家只願提供店內點數", "counterpart_line", "We can only offer store credit.", "The best we can offer is store credit for this item.", "店家提出不能解決需求的替代補償", "誤以為 credit 是原付款退款會錯誤接受方案"),
    ("聽懂推託與條件", "聽懂系統顯示包裹已送達", "counterpart_line", "It was marked as delivered.", "Our system shows the package was marked as delivered yesterday.", "客服以物流系統狀態回應未收件申訴", "若沒理解 marked as delivered，就無法提出反證或要求調查"),
    ("聽懂推託與條件", "聽懂調查還要延長三天", "counterpart_line", "We need three more days.", "We need three more days to investigate your complaint.", "客服延長原本承諾的調查時間", "未聽懂新期限會錯過追蹤時間，也無法表達急迫性"),
    ("聽懂推託與條件", "聽懂店家主張保固不涵蓋損壞", "counterpart_line", "The warranty doesn't cover this damage.", "Unfortunately, the warranty doesn't cover this type of damage.", "店家以保固排除條款拒絕維修", "若沒聽懂 cover 的意思，就無法要求指出具體條款"),
    ("聽懂推託與條件", "聽懂主管目前不在場", "counterpart_line", "The manager isn't available right now.", "The manager isn't available right now, but I can take a message.", "要求升級後，客服表示主管無法立即接手", "沒聽懂替代選項會掛掉而沒有留下升級紀錄"),
    ("聽懂推託與條件", "聽懂回覆時限是五個工作天", "counterpart_line", "You'll hear back within five business days.", "You'll hear back from our team within five business days.", "客服給出以工作天計算的處理時限", "把工作天當日曆天會太早追問或錯過合理升級時點"),
    ("聽懂推託與條件", "聽懂客服查不到先前客訴紀錄", "counterpart_line", "We can't find your complaint record.", "I'm sorry, but we can't find your previous complaint record.", "客服系統沒有顯示先前聯絡紀錄", "若沒有案件編號或證據，可能被迫從頭說明"),
    ("拒絕不合理方案", "政策被拿來推責時拉回本次錯誤", "learner_line", "That policy doesn't address this mistake.", "I understand the policy, but it doesn't address this billing mistake.", "客服只重複一般政策，沒有處理業者造成的錯誤", "若接受泛用政策，具體責任與補救會被迴避"),
    ("拒絕不合理方案", "店內點數不能解決需求時拒絕", "learner_line", "Store credit won't solve the problem.", "I won't shop here again, so store credit won't solve this.", "店家只提供必須再次消費才能使用的點數", "接受點數等於承擔再次消費的風險"),
    ("拒絕不合理方案", "延長調查會造成進一步損失時拒絕", "learner_line", "Three more days isn't workable for me.", "My trip starts tomorrow, so three more days isn't workable.", "客服要求的調查時間超過實際可等待期限", "未說明時間限制，案件會照一般速度排隊"),
    ("拒絕不合理方案", "物流狀態與實際收件衝突時反駁", "learner_line", "Marked delivered doesn't mean it arrived.", "It was marked delivered, but nothing arrived at my building.", "客服把系統送達狀態當作結案依據", "若不區分掃描狀態與實際交付，案件會被直接關閉"),
    ("拒絕不合理方案", "被指控使用不當時說明依規操作", "learner_line", "I followed the care instructions exactly.", "I followed the care instructions exactly, and it still cracked.", "店家暗示損壞是使用者沒有依說明操作", "不澄清使用方式會失去保固或退換資格"),
    ("拒絕不合理方案", "被重複要求補件時指出已提交", "learner_line", "I already sent those photos yesterday.", "I already sent those photos yesterday with the case number.", "客服再次要求昨天已經寄出的相同證據", "重複補件會延誤處理，也掩蓋內部遺失資料"),
    ("拒絕不合理方案", "新客服否認承諾時引用既有承諾", "learner_line", "Your colleague promised me a full refund.", "Your colleague promised a full refund during Monday's call.", "不同客服給出互相矛盾的處理結果", "沒有指出日期與承諾內容，先前決定可能被推翻"),
    ("拒絕不合理方案", "申請被拒時要求具體依據", "learner_line", "Why was my application denied?", "Could you explain which policy clause caused the denial?", "客服只說申請不符合資格，沒有提供原因", "不知道拒絕依據就無法補件、申訴或判斷是否合理"),
    ("升級與追蹤", "第一線無法處理時要求主管", "learner_line", "May I speak with a supervisor?", "Since this remains unresolved, may I speak with a supervisor?", "第一線客服多次重複相同答案且沒有權限", "不升級就無法取得例外處理或更高決策權"),
    ("升級與追蹤", "明確要求把案件升級而非只轉接", "learner_line", "Please escalate this complaint.", "Please escalate this complaint and note the missed deadline.", "問題牽涉承諾逾期，需要正式提高處理層級", "只要求找人談可能沒有留下正式升級紀錄"),
    ("升級與追蹤", "確認案件由誰負責", "learner_line", "Who is responsible for this case?", "Before we finish, who is responsible for following up?", "多人接手但沒有單一窗口", "沒有負責人時每次追問都可能被重新轉介"),
    ("升級與追蹤", "取得案件編號方便後續追蹤", "learner_line", "Could I have the case number?", "Could I have the case number before we end this call?", "客服已建立案件但尚未提供識別碼", "沒有編號就難以證明先前通報或快速查詢"),
    ("升級與追蹤", "要求把口頭承諾寫下來", "learner_line", "Please confirm that in writing.", "Please confirm the refund amount and date in writing.", "客服在電話中承諾退款，但沒有可保存的紀錄", "只有口頭承諾時，後續人員可能否認金額或日期"),
    ("升級與追蹤", "取得明確更新日期", "learner_line", "When should I expect an update?", "You need more time. When should I expect an update?", "客服表示還要調查，卻沒有給下次聯絡時間", "沒有追蹤日期，案件可能無限停留在處理中"),
    ("升級與追蹤", "要求業者說明如何防止再次發生", "learner_line", "How will you prevent this happening again?", "This happened twice. How will you prevent it happening again?", "補救眼前損失後，同一系統性錯誤仍可能重演", "只拿到一次補償，根本原因沒有任何改善承諾"),
]


CURATED = {
    "phone call phobia": (PHONE_CONTRACT, PHONE_SPECS),
    "polite complaints": (COMPLAINT_CONTRACT, COMPLAINT_SPECS),
}


def get_curated_blueprint(topic: str, count: int):
    """Return a complete locked blueprint for an exact supported topic."""
    topic_name = topic.splitlines()[0].strip().casefold()
    entry = CURATED.get(topic_name)
    if not entry:
        return None
    contract, specs = entry
    if count != len(specs):
        return None

    points = []
    for index, spec in enumerate(specs, start=1):
        category, job_key, role_type, phrase, sentence, trigger, stakes = spec
        points.append({
            "id": index,
            "category": category,
            "scenario": trigger,
            "speaker": "對方" if role_type == "counterpart_line" else "學習者",
            "intent": job_key,
            "job_key": job_key,
            "task": sentence,
            "target_phrase": phrase,
            "target_sentence": sentence,
            "pain_trigger": trigger,
            "user_stakes": stakes,
            "desired_outcome": f"當場完成：{job_key}",
            "role_type": role_type,
            "failure_mode": stakes,
            "priority": 5,
            "frequency": 4,
            "friction": 5,
            "sequence": index,
            "score": 93,
            "required_terms": [],
        })
    return dict(contract), points
