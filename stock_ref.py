"""
stock_ref.py — HK Stock Reference Database
============================================
Curated reference for ~130 key stocks: names, industry groupings and signal
type buckets. This is NOT the full trading universe — for the complete stock
universe use ccass_universe.get_universe_codes().

Codes are official HKEX 5-digit codes (identical across HKEX, etnet, CCASS).

Structure:
  STOCKS = {
      "00700": {
          "en":       "TENCENT",           # HKEX English name
          "zh":       "騰訊控股",            # etnet verified Chinese name
          "industry": "TEC",               # etnet industry code
          "ind_zh":   "科技",               # etnet industry Chinese label
          "type":     "bluechip",          # signal threshold bucket
      },
      ...
  }

Industry codes (etnet nature= parameter):
  ETF  ETF基金      BNK  銀行        INS  保險
  TEC  科技         SNS  軟件服務    AUT  汽車
  ENG  能源         UTL  公用事業    REP  地產
  HCR  醫療         IND  工業        MET  金屬礦產
  TEL  電訊         TRN  運輸        RET  零售消費
  CGM  綜合企業     FIN  金融        GEN  其他

Type buckets (for signal thresholds):
  etf        — normal short 40–70%
  bond       — bond ETFs + retail/govt bonds; no signals generated
  derivative — leveraged/inverse/futures products; no signals generated
  stable     — normal short  5–10%  (banks, utilities, energy)
  bluechip   — normal short 10–20%  (large-cap tech, insurance, transport)
  general    — normal short 10–25%  (everything else)
"""

from ccass_universe import normalize_code

STOCKS: dict[str, dict] = {

    # ── ETF ──────────────────────────────────────────────────────────────────
    "02800": {"en":"TRACKER FUND",          "zh":"盈富基金",          "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02828": {"en":"HSCEI ETF",             "zh":"恒生中國企業ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03033": {"en":"CSOP HS TECH",          "zh":"南方恒生科技ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03032": {"en":"PREMIA CHINA ETF",      "zh":"Premia中國新經濟ETF","industry":"ETF","ind_zh":"ETF",    "type":"etf"},
    "03188": {"en":"CSOP A50 ETF",          "zh":"華夏滬深三百ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02846": {"en":"ISHARES HS TECH",       "zh":"iShares恒生科技ETF","industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03140": {"en":"FUTURE HSI ETF",        "zh":"未來恒生指數ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03037": {"en":"HUAXIA HS TECH ETF",    "zh":"華夏恒生科技ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03039": {"en":"E FUND HSI ESG ETF",    "zh":"易方達恒指ESG ETF", "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03011": {"en":"CSOP HSI ETF",          "zh":"南方恒生指數ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02823": {"en":"ISHARES A50",           "zh":"iShares A50 ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03451": {"en":"AGX NASDAQ ETF",        "zh":"AGX納指兌",         "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03190": {"en":"FUBON CSI HS DIV ETF",  "zh":"富邦滬深港高股息",  "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03415": {"en":"AGX SP500 ETF",         "zh":"AGX標普兌",         "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03195": {"en":"HS SP500 ETF",          "zh":"恒生標普五百",      "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03455": {"en":"INVESCO QQQ ETF",       "zh":"景順QQQ",           "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03111": {"en":"E FUND CSI A50 ETF",    "zh":"易方達A50",          "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    # ── 主題 ETF（截短名稱不含 ETF 字眼，須靠代碼列表攔截）────────────────────────
    "03460": {"en":"CHINA AMC SOL ETF",              "zh":"華夏SOL ETF",        "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03489": {"en":"E FUND AI ETF",                  "zh":"易方達AI ETF",        "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03076": {"en":"FUBON TAIWAN SEMICONDUCTOR ETF", "zh":"富邦台灣半導體ETF",   "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03468": {"en":"CHINA AMC MSCI CHINA ETF",       "zh":"華夏MSCI中國ETF",    "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03462": {"en":"BOSERA MSCI CHINA A ETF",        "zh":"博時MSCI A股ETF",    "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03465": {"en":"HARVEST MSCI CHINA A ETF",       "zh":"嘉實MSCI A股ETF",    "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03466": {"en":"HANG SENG HIGH DIV YIELD ETF",   "zh":"恒生高息股ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03470": {"en":"GX CHINA INNOVATION ETF",        "zh":"GX中國創新ETF",      "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03488": {"en":"E FUND MSCI CHINA A ETF",        "zh":"易方達MSCI A股ETF",  "industry":"ETF","ind_zh":"ETF","type":"etf"},
    # ── 加密貨幣 ETF ─────────────────────────────────────────────────────────
    "03008": {"en":"BOSERA HASHKEY BITCOIN ETF", "zh":"博時HashKey比特幣ETF","industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03042": {"en":"CSOP BITCOIN ETF",           "zh":"南方比特幣ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03046": {"en":"CSOP ETHER ETF",             "zh":"南方以太幣ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03135": {"en":"FA SAMSUNG BITCOIN ETF",     "zh":"FA三星比特幣ETF",     "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03179": {"en":"HARVEST BITCOIN ETF",        "zh":"嘉實比特幣ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03325": {"en":"CHINA AMC BITCOIN ETF",      "zh":"華夏比特幣ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03326": {"en":"CHINA AMC ETHER ETF",        "zh":"華夏以太幣ETF",       "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03439": {"en":"HASHKEY BITCOIN ETF",        "zh":"HashKey比特幣ETF",    "industry":"ETF","ind_zh":"ETF","type":"etf"},
    "03440": {"en":"HASHKEY ETHER ETF",          "zh":"HashKey以太幣ETF",    "industry":"ETF","ind_zh":"ETF","type":"etf"},
    # ── 債券 / 國債 ───────────────────────────────────────────────────────────
    "02829": {"en":"ISHARES CHINA GOVT BOND","zh":"安碩中國國債ETF",  "industry":"ETF","ind_zh":"債券",     "type":"bond"},
    "03108": {"en":"PREMIA ESG BOND ETF",   "zh":"嗎實ESG領先債券ETF","industry":"ETF","ind_zh":"債券",     "type":"bond"},
    # ── 貨幣市場 ETF ─────────────────────────────────────────────────────────
    "03196": {"en":"BOSERA USD MONEY MARKET ETF",    "zh":"A博時美元",         "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03197": {"en":"HARVEST USD MONEY MARKET ETF",   "zh":"嘉實美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03477": {"en":"BOCOM USD MONEY MARKET ETF",     "zh":"交銀美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03918": {"en":"CMBI USD MONEY MARKET ETF",      "zh":"招銀國際美元貨幣",  "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03919": {"en":"BOCI USD MONEY MARKET ETF",      "zh":"中銀國際美元貨幣",  "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03960": {"en":"CHINA AMC USD MONEY MARKET ETF", "zh":"華夏美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03053": {"en":"CSOP USD MONEY MARKET ETF",      "zh":"南方美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03085": {"en":"HSBCMF USD MONEY MARKET ETF",    "zh":"滙豐美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03109": {"en":"CMS USD MONEY MARKET ETF",       "zh":"招商美元貨幣ETF",   "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03077": {"en":"ICBC CSOP USD MONEY MARKET ETF", "zh":"工銀南方美元貨幣",  "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03018": {"en":"CSOP CHINA USD BOND ETF",        "zh":"南方中國美元債ETF",  "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03126": {"en":"CSOP ASIA IG BOND ETF",          "zh":"南方亞洲投資級債ETF","industry":"ETF","ind_zh":"債券","type":"bond"},
    "03139": {"en":"HUAAN ASIA HY BOND ETF",         "zh":"華安亞洲高收益債ETF","industry":"ETF","ind_zh":"債券","type":"bond"},
    "03047": {"en":"ABERDEEN STD USD BOND",          "zh":"安本標準美元債",     "industry":"ETF","ind_zh":"債券","type":"bond"},
    "03080": {"en":"FULLERTON SGD CASH FUND",        "zh":"富敦新加坡元現金基金","industry":"ETF","ind_zh":"債券","type":"bond"},
    "03430": {"en":"MBC BITCOIN ETF",                    "zh":"MBC比特幣ETF",       "industry":"ETF","ind_zh":"ETF",  "type":"etf"},
    "03431": {"en":"MBC ETHER ETF",                      "zh":"MBC以太幣ETF",       "industry":"ETF","ind_zh":"ETF",  "type":"etf"},
    "03425": {"en":"MBC ETHER ETF II",                   "zh":"MBC以太幣ETF II",    "industry":"ETF","ind_zh":"ETF",  "type":"etf"},
    "03472": {"en":"CHINA AMC USD MONEY MARKET ETF",     "zh":"A華夏美元數",        "industry":"ETF","ind_zh":"債券", "type":"bond"},
    # ── HKD 貨幣市場 ETF ─────────────────────────────────────────────────────
    "03471": {"en":"CHINA AMC HKD MONEY MARKET ETF",     "zh":"A華夏港元數",        "industry":"ETF","ind_zh":"債券", "type":"bond"},
    "03157": {"en":"CSOP HKD MONEY MARKET ETF",          "zh":"南方港元貨幣ETF",    "industry":"ETF","ind_zh":"債券", "type":"bond"},
    "03158": {"en":"BOSERA HKD MONEY MARKET ETF",        "zh":"博時港元貨幣ETF",    "industry":"ETF","ind_zh":"債券", "type":"bond"},
    "03173": {"en":"HARVEST HKD MONEY MARKET ETF",       "zh":"嘉實港元貨幣ETF",    "industry":"ETF","ind_zh":"債券", "type":"bond"},
    "03167": {"en":"PING AN HKD MONEY MARKET ETF",       "zh":"平安港元貨幣ETF",    "industry":"ETF","ind_zh":"債券", "type":"bond"},
    "03490": {"en":"E FUND HKD MONEY MARKET ETF",        "zh":"易方達港元貨幣ETF",  "industry":"ETF","ind_zh":"債券", "type":"bond"},
    # ── 主動基金 / 資產管理公司 ────────────────────────────────────────────────
    "03406": {"en":"PING AN TECHNOLOGY SELECT FUND", "zh":"平安科技精選",       "industry":"ETF","ind_zh":"基金","type":"etf"},
    "03396": {"en":"PING AN ASSET MANAGEMENT",       "zh":"平安資管",           "industry":"ETF","ind_zh":"基金","type":"etf"},
    "03448": {"en":"BOSERA FUNDS",                   "zh":"博時基金",           "industry":"ETF","ind_zh":"基金","type":"etf"},
    "03474": {"en":"CHINA SOUTHERN ASSET MANAGEMENT","zh":"南方資管",           "industry":"ETF","ind_zh":"基金","type":"etf"},
    "03476": {"en":"E FUND MANAGEMENT",              "zh":"易方達資管",         "industry":"ETF","ind_zh":"基金","type":"etf"},
    "03487": {"en":"HARVEST FUND MANAGEMENT",        "zh":"嘉實資管",           "industry":"ETF","ind_zh":"基金","type":"etf"},

    # ── 銀行 ─────────────────────────────────────────────────────────────────
    "00005": {"en":"HSBC HOLDINGS",         "zh":"滙豐控股",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00011": {"en":"HANG SENG BANK",        "zh":"恒生銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00023": {"en":"BANK OF E ASIA",        "zh":"東亞銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00388": {"en":"HKEX",                  "zh":"香港交易所",        "industry":"FIN","ind_zh":"金融",     "type":"bluechip"},
    "00939": {"en":"CCB",                   "zh":"建設銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "01398": {"en":"ICBC",                  "zh":"工商銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "01288": {"en":"ABC",                   "zh":"農業銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "02388": {"en":"BOC HK",                "zh":"中銀香港",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "03328": {"en":"BOCOM",                 "zh":"交通銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "03988": {"en":"BANK OF CHINA",         "zh":"中國銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},

    # ── 保險 ─────────────────────────────────────────────────────────────────
    "01299": {"en":"AIA",                   "zh":"友邦保險",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02318": {"en":"PING AN",               "zh":"中國平安",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02628": {"en":"CHINA LIFE",            "zh":"中國人壽",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02328": {"en":"PICC P&C",              "zh":"中國財險",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "00945": {"en":"MANULIFE",              "zh":"宏利金融",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "06161": {"en":"CHINA TAIPING",         "zh":"中國太平",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02378": {"en":"PRUDENTIAL",            "zh":"保誠",              "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02338": {"en":"PICC GROUP",             "zh":"中國人民保險集團",  "industry":"INS","ind_zh":"保險",     "type":"bluechip"},

    # ── 科技平台 ─────────────────────────────────────────────────────────────
    "00700": {"en":"TENCENT",               "zh":"騰訊控股",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09988": {"en":"BABA-W",                "zh":"阿里巴巴",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "00981": {"en":"SMIC",                   "zh":"中芯國際",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "01810": {"en":"XIAOMI-W",              "zh":"小米集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09618": {"en":"JD-SW",                 "zh":"京東集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09888": {"en":"BAIDU-SW",              "zh":"百度集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09999": {"en":"NETEASE-S",             "zh":"網易",              "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "03690": {"en":"MEITUAN-W",             "zh":"美團",              "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "01024": {"en":"KUAISHOU-W",            "zh":"快手",              "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09961": {"en":"TRIP-SW",               "zh":"攜程集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09626": {"en":"BILIBILI-SW",           "zh":"嗶哩嗶哩",          "industry":"TEC","ind_zh":"科技",     "type":"general"},
    "00020": {"en":"SENSETIME-W",           "zh":"商湯科技",          "industry":"TEC","ind_zh":"科技",     "type":"general"},
    "02382": {"en":"SUNNY OPT.",            "zh":"舜宇光學科技",      "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "03750": {"en":"CATL",                  "zh":"寧德時代",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09660": {"en":"HORIZONROBOT-W",         "zh":"地平線機器人-W",    "industry":"TEC","ind_zh":"科技",     "type":"general"},
    "09866": {"en":"NIO-SW",                 "zh":"蔚來",              "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "09901": {"en":"NEW ORIENTAL-S",         "zh":"新東方",            "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},

    # ── 軟件服務 ─────────────────────────────────────────────────────────────
    "00241": {"en":"ALI HEALTH",            "zh":"阿里健康",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},
    "06618": {"en":"JD HEALTH",             "zh":"京東健康",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},
    "00354": {"en":"CHINASOFT INT'L",       "zh":"中軟國際",          "industry":"SNS","ind_zh":"軟件服務", "type":"general"},
    "00992": {"en":"LENOVO",                "zh":"聯想集團",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},

    # ── 汽車 ─────────────────────────────────────────────────────────────────
    "01211": {"en":"BYD",                   "zh":"比亞迪股份",        "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "00175": {"en":"GEELY AUTO",            "zh":"吉利汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "02015": {"en":"LI AUTO-W",             "zh":"理想汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "09868": {"en":"XPENG-W",               "zh":"小鵬汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "02238": {"en":"GAC GROUP",             "zh":"廣汽集團",          "industry":"AUT","ind_zh":"汽車",     "type":"general"},

    # ── 能源 ─────────────────────────────────────────────────────────────────
    "00883": {"en":"CNOOC",                 "zh":"中國海洋石油",      "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00386": {"en":"SINOPEC CORP",          "zh":"中國石化",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00857": {"en":"PETROCHINA",            "zh":"中國石油股份",      "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00135": {"en":"KUNLUN ENERGY",         "zh":"崑崙能源",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00384": {"en":"CHINA GAS HOLD",        "zh":"中國燃氣",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "01193": {"en":"CR GAS",                "zh":"華潤燃氣",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "02688": {"en":"ENN ENERGY",            "zh":"新奧能源",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00968": {"en":"XINYI SOLAR",           "zh":"信義光能",          "industry":"ENG","ind_zh":"能源",     "type":"general"},

    # ── 公用事業 ─────────────────────────────────────────────────────────────
    "00002": {"en":"CLP HOLDINGS",          "zh":"中電控股",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00003": {"en":"HK & CHINA GAS",        "zh":"香港中華煤氣",      "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00006": {"en":"POWER ASSETS",          "zh":"電能實業",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00066": {"en":"MTR CORPORATION",       "zh":"港鐵公司",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00855": {"en":"CHINA WATER AFF",        "zh":"中國水務",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00270": {"en":"GUANGDONG INV",          "zh":"粵海投資",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00177": {"en":"JIANGSU EXP",            "zh":"江蘇高速",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00107": {"en":"SICHUAN EXP",            "zh":"四川成渝高速",      "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00995": {"en":"ANHUI EXP",              "zh":"安徽高速",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00941": {"en":"CHINA MOBILE",          "zh":"中國移動",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "00762": {"en":"CHINA UNICOM",          "zh":"中國聯通",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "00008": {"en":"PCCW",                  "zh":"電訊盈科",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "01816": {"en":"CHINA TELECOM",          "zh":"中國電信",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "00763": {"en":"ZTE CORP",               "zh":"中興通訊",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},

    # ── 地產 ─────────────────────────────────────────────────────────────────
    "00016": {"en":"SHK PPT",               "zh":"新鴻基地產",        "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00012": {"en":"HENDERSON LAND",        "zh":"恒基地產",          "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00017": {"en":"NEW WORLD DEV",         "zh":"新世界發展",        "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00083": {"en":"SINO LAND",             "zh":"信和置業",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00101": {"en":"HANG LUNG PPT",         "zh":"恒隆地產",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00014": {"en":"HYSAN DEV",             "zh":"希慎興業",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "01109": {"en":"CR LAND",               "zh":"華潤置地",          "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00960": {"en":"LONGFOR PPT",           "zh":"龍湖集團",          "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00823": {"en":"LINK REIT",             "zh":"領展房產基金",      "industry":"REP","ind_zh":"地產",     "type":"stable"},

    # ── 醫療 ─────────────────────────────────────────────────────────────────
    "01177": {"en":"SINO BIOPHARM",         "zh":"中國生物製藥",      "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "01093": {"en":"CSPC PHARMA",           "zh":"石藥集團",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "02269": {"en":"WUXI BIO",              "zh":"藥明生物",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "02359": {"en":"WUXI APPTEC",           "zh":"藥明康德",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "06160": {"en":"BEIGENE-SW",            "zh":"百濟神州",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "00013": {"en":"HUTCHMED",              "zh":"和黃醫藥",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},

    # ── 工業 ─────────────────────────────────────────────────────────────────
    "06869": {"en":"YANGTZE OFC",             "zh":"長飛光纖光纜",       "industry":"IND","ind_zh":"工業",     "type":"general"},
    "00390": {"en":"CHINA RAILWAY",         "zh":"中國中鐵",          "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "01186": {"en":"CR CONSTRUCTION",       "zh":"中國鐵建",          "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "01800": {"en":"CHINA COMM CONST",       "zh":"中國交通建設",      "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "03969": {"en":"CRRC CORP",              "zh":"中國中車",          "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "06690": {"en":"HAIER SMART HOME",      "zh":"海爾智家",          "industry":"IND","ind_zh":"工業",     "type":"general"},
    "02313": {"en":"SHENZHOU INTL",          "zh":"申洲國際",          "industry":"IND","ind_zh":"工業",     "type":"bluechip"},
    "00669": {"en":"TECHTRONIC IND",          "zh":"創科實業",          "industry":"IND","ind_zh":"工業",     "type":"bluechip"},

    # ── 金屬礦產 ─────────────────────────────────────────────────────────────
    "02899": {"en":"CHALCO",                  "zh":"中國鋁業",           "industry":"MET","ind_zh":"金屬礦產", "type":"general"},
    "01088": {"en":"CHINA SHENHUA",         "zh":"中國神華",          "industry":"MET","ind_zh":"金屬礦產", "type":"stable"},
    "00358": {"en":"JIANGXI COPPER",        "zh":"江西銅業股份",      "industry":"MET","ind_zh":"金屬礦產", "type":"general"},

    # ── 運輸物流 ─────────────────────────────────────────────────────────────
    "00293": {"en":"CATHAY PAC AIR",        "zh":"國泰航空",          "industry":"TRN","ind_zh":"運輸",     "type":"bluechip"},
    "00316": {"en":"OOIL",                  "zh":"東方海外國際",      "industry":"TRN","ind_zh":"運輸",     "type":"general"},
    "01199": {"en":"COSCO SHIPPING",        "zh":"中遠海控",          "industry":"TRN","ind_zh":"運輸",     "type":"general"},
    "00144": {"en":"CM PORT",               "zh":"招商局港口",        "industry":"TRN","ind_zh":"運輸",     "type":"general"},
    "00753": {"en":"AIR CHINA",              "zh":"中國國航",          "industry":"TRN","ind_zh":"運輸",     "type":"bluechip"},
    "00670": {"en":"CHINA EASTERN AIR",      "zh":"中國東方航空",      "industry":"TRN","ind_zh":"運輸",     "type":"bluechip"},

    # ── 零售消費 ─────────────────────────────────────────────────────────────
    "02020": {"en":"ANTA SPORTS",            "zh":"安踏體育",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "02319": {"en":"MENGNIU",               "zh":"蒙牛乳業",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00151": {"en":"WANT WANT CHINA",       "zh":"旺旺中國",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00288": {"en":"WH GROUP",              "zh":"萬洲國際",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00027": {"en":"GALAXY ENT",            "zh":"銀河娛樂",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "01928": {"en":"SANDS CHINA",           "zh":"金沙中國",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "09633": {"en":"NONGFU SPRING",         "zh":"農夫山泉",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "06862": {"en":"HAIDILAO",              "zh":"海底撈",            "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "01876": {"en":"BUDWEISER APAC",        "zh":"百威亞太",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "00168": {"en":"TSINGTAO BREW",         "zh":"青島啤酒股份",      "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00291": {"en":"CR BEER",               "zh":"華潤啤酒",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "00136": {"en":"CHINA RUYI",            "zh":"中國如意",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00189": {"en":"DONGYUE GROUP",         "zh":"東岳集團",          "industry":"RET","ind_zh":"零售消費", "type":"general"},

    # ── 綜合企業 ─────────────────────────────────────────────────────────────
    "00001": {"en":"CKH HOLDINGS",          "zh":"長和",              "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "00019": {"en":"SWIRE PACIFIC A",       "zh":"太古股份Ａ",        "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "00267": {"en":"CITIC",                 "zh":"中信股份",          "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "01038": {"en":"CKI HOLDINGS",           "zh":"長江基建",          "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "06098": {"en":"CR MIXC",                "zh":"華潤萬象生活",      "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
}

# ── Lookup helpers ────────────────────────────────────────────────────────────

def get_zh_name(code: str) -> str | None:
    entry = STOCKS.get(normalize_code(code))
    return entry["zh"] if entry else None

def get_en_name(code: str) -> str | None:
    entry = STOCKS.get(normalize_code(code))
    return entry["en"] if entry else None

def get_industry(code: str) -> tuple[str, str]:
    entry = STOCKS.get(normalize_code(code))
    return (entry["industry"], entry["ind_zh"]) if entry else ("GEN", "其他")

def get_type(code: str) -> str | None:
    entry = STOCKS.get(normalize_code(code))
    return entry["type"] if entry else None

def get_stock_info(code: str) -> dict:
    code5 = normalize_code(code)
    entry = STOCKS.get(code5, {})
    return {
        "code":        code5,
        "en":          entry.get("en", ""),
        "zh":          entry.get("zh", ""),
        "industry":    entry.get("industry", "GEN"),
        "ind_zh":      entry.get("ind_zh", "其他"),
        "type":        entry.get("type", "general"),
    }
