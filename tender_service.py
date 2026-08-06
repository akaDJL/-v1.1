#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
居间小助手 · 后端采集服务（零第三方依赖，仅标准库）
====================================================
功能：
  1. 自动抓招标公告 / 中标候选·结果公告（多源适配器：全国公共资源 / 中国政府采购 / 各省平台）
  2. 竞标信息：中标公告里的实际中标人、中标金额、候选人（来自 mock / 真实抓取标注）
  3. 相关体量：按金额推断所需资质等级与企业规模建议
  4. 可能参选单位：基于企业库，按区域/资质/历史同类/体量适配打分推断

运行（本机无网时跑示例，有网主机上跑真实）：
  python tender_service.py                      # mock 模式（离线可用，演示闭环）
  LIVE=True python tender_service.py            # 真实抓取（需联网主机，并校准选择器）
  PORT=8000 LIVE=True DELAY=1.5 python tender_service.py

接口：
  GET /api/tenders?region=山西&keyword=公路&type=all
  GET /api/candidates?tender_id=0
  GET /api/enterprises?region=山西
  GET /api/health

合规：仅聚合政府信息公开的招标/中标公告 + 企业工商公开字段；不爬聊天、不深挖个人手机号。
     真实抓取务必遵守目标站点 robots 与访问频率限制，DELAY 设大一些，别硬刷。
"""

import json
import re
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import socketserver
import http.server
import threading
from datetime import date, timedelta
import ssl

# 部分政府站点证书较旧/域名不匹配，抓取时忽略主机校验（仅用于采集公开公告）
_SSL_IGNORE = ssl.create_default_context()
_SSL_IGNORE.check_hostname = False
_SSL_IGNORE.verify_mode = ssl.CERT_NONE

# ============================ 配置 ============================
LIVE = os.environ.get("LIVE", "False").lower() in ("1", "true", "yes")
PORT = int(os.environ.get("PORT", "9000"))  # 阿里云 FC 约定 9000；本地可覆盖
DELAY = float(os.environ.get("DELAY", "1.0"))          # 每次请求间隔（秒），真实抓取请 ≥1
TIMEOUT = int(os.environ.get("TIMEOUT", "15"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

def _random_ua():
    import random as _r
    return UA_POOL[_r.randint(0, len(UA_POOL) - 1)]

# ====================== 关键词词库（全工程领域） ======================
KEYWORD_POOL = ["环保","EPC","污水处理","生态修复","垃圾焚烧","扬尘治理","脱硫脱硝","市政","管网","管廊",
    "供热","供水","燃气","公路","桥梁","隧道","铁路","轨道交通","交通","水利","水库","水电站","电力",
    "输变电","光伏","风电","新能源","石化","化工","炼油","机电","安装","钢结构","冶金","矿山","煤矿",
    "房建","建筑","医院","学校","棚改","安置房","机场","港口","航道","通信",
    "泵站","水厂","水渠","管涵","闸","改造","升级","新建","扩建","土建","配电","变电站","线路",
    "路基","路面","标段","厂区","车间","基坑","桩基","河道","堤防","水坝","综合管廊","给排水",
    "雨污","消防","绿化","环卫","基础设施","搬迁","整治","治理"]

# 省级行政区（用于从标题识别区域）
PROVINCES = ["北京","天津","河北","山西","内蒙古","辽宁","吉林","黑龙江","上海","江苏","浙江","安徽","福建",
    "江西","山东","河南","湖北","湖南","广东","广西","海南","重庆","四川","贵州","云南","西藏","陕西",
    "甘肃","青海","宁夏","新疆","香港","澳门","台湾"]

# ====================== 内置企业库（示例，落地替换为企查查真实字段） ======================
ENTERPRISES = [
    {"id": 1, "name": "山西建设投资集团有限公司", "region": "山西太原", "reg_capital": 500000, "employees": 20000,
     "qual": ["市政公用工程施工总承包特级", "建筑工程施工总承包特级", "环保工程专业承包壹级"],
     "history": ["市政", "房建", "EPC", "污水处理", "生态修复"]},
    {"id": 2, "name": "山西省工业设备安装集团有限公司", "region": "山西太原", "reg_capital": 120000, "employees": 3500,
     "qual": ["机电工程施工总承包壹级", "环保工程专业承包壹级", "钢结构工程专业承包壹级"],
     "history": ["机电", "安装", "环保", "石化", "EPC"]},
    {"id": 3, "name": "中化二建集团有限公司", "region": "山西太原", "reg_capital": 150000, "employees": 6000,
     "qual": ["石油化工工程施工总承包特级", "环保工程专业承包壹级"],
     "history": ["石化", "化工", "炼油", "环保", "EPC"]},
    {"id": 4, "name": "山西水务投资集团有限公司", "region": "山西太原", "reg_capital": 300000, "employees": 1500,
     "qual": ["水利水电工程施工总承包壹级", "市政公用工程总承包壹级"],
     "history": ["水利", "水库", "供水", "污水处理", "市政"]},
    {"id": 5, "name": "山西大地环境投资控股有限公司", "region": "山西太原", "reg_capital": 200000, "employees": 800,
     "qual": ["生态修复", "环保工程专业承包壹级"],
     "history": ["生态修复", "环保", "市政"]},
    {"id": 6, "name": "山西交控生态环境科技有限公司", "region": "山西太原", "reg_capital": 50000, "employees": 600,
     "qual": ["环保工程专业承包壹级", "市政公用工程总承包贰级"],
     "history": ["环保", "生态修复", "扬尘治理"]},
    {"id": 7, "name": "山西尚风科技股份有限公司", "region": "山西太原", "reg_capital": 12000, "employees": 400,
     "qual": ["环保工程专业承包贰级", "扬尘治理专项"],
     "history": ["扬尘治理", "环保"]},
    {"id": 8, "name": "太原市政建设集团有限公司", "region": "山西太原", "reg_capital": 80000, "employees": 2500,
     "qual": ["市政公用工程施工总承包壹级"],
     "history": ["市政", "污水处理", "EPC", "管网"]},
    {"id": 9, "name": "中铁十二局集团有限公司", "region": "山西太原", "reg_capital": 600000, "employees": 30000,
     "qual": ["铁路工程施工总承包特级", "市政公用工程总承包特级", "环保工程专业承包壹级"],
     "history": ["铁路", "公路", "市政", "EPC"]},
    {"id": 10, "name": "山西四建集团有限公司", "region": "山西太原", "reg_capital": 100000, "employees": 5000,
     "qual": ["建筑工程施工总承包特级", "市政公用工程总承包壹级", "环保工程专业承包壹级"],
     "history": ["房建", "建筑", "EPC", "市政"]},
    {"id": 11, "name": "山西路桥建设集团有限公司", "region": "山西太原", "reg_capital": 300000, "employees": 6000,
     "qual": ["公路工程施工总承包特级", "桥梁工程专业承包壹级"],
     "history": ["公路", "桥梁", "交通"]},
    {"id": 12, "name": "中铁三局集团有限公司", "region": "山西太原", "reg_capital": 500000, "employees": 28000,
     "qual": ["铁路工程施工总承包特级", "公路工程施工总承包特级", "市政公用工程总承包特级"],
     "history": ["铁路", "公路", "市政", "桥梁", "EPC"]},
    {"id": 13, "name": "中铁十七局集团有限公司", "region": "山西太原", "reg_capital": 400000, "employees": 22000,
     "qual": ["铁路工程施工总承包特级", "公路工程施工总承包壹级"],
     "history": ["铁路", "公路", "市政"]},
    {"id": 14, "name": "山西八建集团有限公司", "region": "山西太原", "reg_capital": 100000, "employees": 4500,
     "qual": ["建筑工程施工总承包壹级", "钢结构工程专业承包壹级"],
     "history": ["房建", "建筑", "钢结构", "EPC"]},
    {"id": 15, "name": "山西一建集团有限公司", "region": "山西太原", "reg_capital": 80000, "employees": 3800,
     "qual": ["建筑工程施工总承包壹级"],
     "history": ["房建", "建筑", "安置房"]},
    {"id": 16, "name": "中国建筑第三工程局有限公司", "region": "湖北武汉", "reg_capital": 800000, "employees": 40000,
     "qual": ["建筑工程施工总承包特级", "市政公用工程总承包特级"],
     "history": ["房建", "建筑", "市政", "EPC", "医院"]},
    {"id": 17, "name": "中国建筑第八工程局有限公司", "region": "上海", "reg_capital": 1000000, "employees": 45000,
     "qual": ["建筑工程施工总承包特级", "市政公用工程总承包特级"],
     "history": ["房建", "建筑", "机场", "EPC"]},
    {"id": 18, "name": "中交第一公路工程局有限公司", "region": "北京", "reg_capital": 600000, "employees": 25000,
     "qual": ["公路工程施工总承包特级", "桥梁工程专业承包壹级"],
     "history": ["公路", "桥梁", "交通", "EPC"]},
    {"id": 19, "name": "中交第二公路工程局有限公司", "region": "陕西西安", "reg_capital": 500000, "employees": 20000,
     "qual": ["公路工程施工总承包特级", "隧道工程专业承包壹级"],
     "history": ["公路", "桥梁", "隧道", "交通"]},
    {"id": 20, "name": "中国电建集团", "region": "北京", "reg_capital": 2000000, "employees": 120000,
     "qual": ["水利水电工程施工总承包特级", "电力工程施工总承包特级"],
     "history": ["水利", "水电站", "电力", "新能源", "光伏", "风电"]},
    {"id": 21, "name": "中国能源建设集团山西电力建设有限公司", "region": "山西太原", "reg_capital": 100000, "employees": 5000,
     "qual": ["电力工程施工总承包壹级", "机电工程施工总承包壹级"],
     "history": ["电力", "输变电", "机电", "安装"]},
    {"id": 22, "name": "中煤建筑安装工程集团有限公司", "region": "河北邯郸", "reg_capital": 300000, "employees": 12000,
     "qual": ["矿山工程施工总承包特级", "建筑工程施工总承包壹级"],
     "history": ["矿山", "煤矿", "房建", "安装"]},
    {"id": 23, "name": "山西焦煤集团", "region": "山西太原", "reg_capital": 1000000, "employees": 80000,
     "qual": ["矿山工程施工总承包壹级"],
     "history": ["矿山", "煤矿"]},
    {"id": 24, "name": "山西能源集团有限公司", "region": "山西太原", "reg_capital": 400000, "employees": 6000,
     "qual": ["电力工程施工总承包壹级"],
     "history": ["电力", "新能源", "光伏", "风电"]},
    {"id": 25, "name": "山西二建集团有限公司", "region": "山西太原", "reg_capital": 90000, "employees": 4000,
     "qual": ["建筑工程施工总承包壹级"],
     "history": ["房建", "建筑", "市政"]},
    {"id": 26, "name": "山西建筑工程集团有限公司", "region": "山西太原", "reg_capital": 150000, "employees": 7000,
     "qual": ["建筑工程施工总承包特级", "市政公用工程总承包壹级"],
     "history": ["房建", "建筑", "市政", "EPC"]},
    {"id": 27, "name": "太原钢铁(集团)建设有限公司", "region": "山西太原", "reg_capital": 60000, "employees": 3000,
     "qual": ["冶金工程施工总承包壹级", "建筑工程施工总承包壹级"],
     "history": ["冶金", "房建", "钢结构"]},
    {"id": 28, "name": "山西机械化建设集团有限公司", "region": "山西太原", "reg_capital": 70000, "employees": 3000,
     "qual": ["公路工程施工总承包壹级", "市政公用工程总承包壹级"],
     "history": ["公路", "市政", "机场", "场平"]},
    {"id": 29, "name": "大同市政建设发展有限公司", "region": "山西大同", "reg_capital": 30000, "employees": 1500,
     "qual": ["市政公用工程施工总承包壹级"],
     "history": ["市政", "管网", "供热"]},
    {"id": 30, "name": "山西五建集团有限公司", "region": "山西太原", "reg_capital": 85000, "employees": 3800,
     "qual": ["建筑工程施工总承包壹级", "市政公用工程总承包壹级"],
     "history": ["房建", "建筑", "市政"]},
]

# 通用解析：从列表页 HTML 抽出公告链接 + 标题
RE_A = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{6,140})</a>', re.I)
RE_DATE = re.compile(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})')


def _kw(text):
    return [k for k in KEYWORD_POOL if k in (text or "")]


# 公告类标识词：仅保留真实公告，过滤栏目/机构/导航链接（避免抓到"XX厅""XX局"）
NOTICE_KW = ["招标", "采购", "中标", "成交", "磋商", "谈判", "询价",
             "单一来源", "资格预审", "比选", "废标", "终止", "拟采购", "征集", "结果"]


def _is_notice(title):
    if any(k in title for k in NOTICE_KW):
        return True
    if "公告" in title and _kw(title):   # 含"公告"且命中工程词，视为真实公告
        return True
    return False


# 货物/服务噪声词：工程居间排除纯货物与后勤服务采购（这些不是工程居间标的）
GOODS_NOISE = ["无人机", "电脑", "软件", "维保", "耗材", "试剂", "图书", "家具",
               "服装", "食品", "打印机", "相机", "复印机", "服务器", "医疗器械",
               "药品", "被服", "车辆购置", "物业管理", "食材", "餐饮", "租赁",
               "教学设备", "实验室设备", "仪器设备", "卫星转发器", "网络安全设备",
               "运维服务", "保洁", "保安", "培训", "宣传", "印刷", "办公用品",
               "监测设备", "电子卖场", "网上商城",
               "智慧黑板", "消防器材", "云终端", "云资源", "终端", "眼科", "实验室",
               "苗木", "苗圃", "林场", "种植", "养护", "绿地", "游园", "培育", "药材",
               "猪苓", "仿真", "融媒体", "测量类", "地质勘查", "矿产勘查", "物资",
               "货物", "器材", "黑板", "补遗", "数据中心", "产品采购", "系统采购",
               "水泵", "设备采购", "设备更新", "果树", "造林", "苗木采购"]


def _is_goods_noise(title):
    return any(k in title for k in GOODS_NOISE)


# 工程实质词：工程居间聚焦施工/总承包/EPC类，过滤纯货物/服务/研究采购
ENGINEER_KW = ["工程", "施工", "总承包", "EPC", "标段", "监理", "勘查", "安装", "改造"]


def _detect_region(text):
    for p in PROVINCES:
        if p in (text or ""):
            return p
    return ""


# ====================== 数据源（适配器） ======================
class BaseSource:
    kind = "tender"

    def fetch(self, region, keyword, pages=2):
        raise NotImplementedError


class GenericListSource(BaseSource):
    """真实采集基类：针对服务端渲染的政府/公共资源列表页，通用解析 <a> 公告链接。
    只保留命中工程关键词的条目（避免抓到无关政务链接）。详情页二次抓取补金额/联系人（按需开启）。
    选择器为通用策略，不同站点结构差异较大，LIVE 上线前请在该站点实际页面校准。"""
    LIST_URLS = []

    def _page_url(self, base, p):
        # 多数列表页支持 ?pn= 或 _pN.html；子类可覆盖
        return base if p <= 1 else (base + ("&" if "?" in base else "?") + "pn=%d" % p)

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": _random_ua(), "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "ignore")

    def _clean(self, s):
        return re.sub(r"\s+", "", s).strip()

    def _parse_list(self, html, base, region):
        out = []
        seen = set()
        for m in RE_A.finditer(html):
            href, raw_title = m.group(1), m.group(2)
            title = self._clean(raw_title)
            if not title or len(title) < 6:
                continue
            kws = _kw(title)
            if not kws:                       # 仅保留工程相关
                continue
            if not _is_notice(title):         # 仅保留真实公告，过滤机构/导航链接
                continue
            if _is_goods_noise(title):         # 排除纯货物/服务采购噪声
                continue
            # 跳过明显导航/栏目链接
            if title in ("首页", "上一页", "下一页", "更多", "返回") or "办事" in title:
                continue
            url = urllib.parse.urljoin(base, href)
            if url in seen:
                continue
            seen.add(url)
            det_region = _detect_region(title) or ""
            out.append({
                "title": title, "region": det_region, "amount_wan": 0,
                "publish_date": "", "buyer": "", "contact": "", "url": url,
                "type": "tender", "qual": [], "keywords": kws,
            })
        return out

    def fetch(self, region, keyword, pages=2):
        if not LIVE:
            return []
        items = []
        for base in self.LIST_URLS:
            for p in range(1, max(1, pages) + 1):
                try:
                    html = self._get(self._page_url(base, p))
                    for it in self._parse_list(html, base, region):
                        if keyword:
                            hay = it["title"] + " " + " ".join(it["keywords"])
                            if keyword not in hay:
                                continue
                        items.append(it)
                except Exception as e:
                    print("[%s] 抓取失败 %s: %s" % (self.__class__.__name__, base, e))
                time.sleep(DELAY)
        return items


class ShanxiTenderSource(GenericListSource):
    """山西省政府采购网 / 省公共资源交易平台。LIVE 时生效。
    证书较旧→忽略主机校验；域名可能随站点改版变化，LIST_URLS 多候选容错，失败自动跳过。"""
    LIST_URLS = [
        "https://www.ccgp-shanxi.gov.cn/cggg/zygg/",
        "http://www.ccgp-shanxi.gov.cn/cggg/dfgg/",
        "http://prec.sxzwfw.gov.cn/",
        "https://ggzy.sxzwfw.gov.cn/",
    ]

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": _random_ua(), "Accept-Language": "zh-CN,zh;q=0.9"})
        ctx = _SSL_IGNORE if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return resp.read().decode("utf-8", "ignore")


class GGZYNationalSource(GenericListSource):
    """全国公共资源交易平台（工程建设频道）。
    首页服务端渲染最新工程建设公告（/information/deal/html/a/{省码}/{栏目}/{日期}/{id}.html），
    覆盖全国各省各行业工程；列表页为 JS 渲染无法静态抓，故以首页作入口。"""
    LIST_URLS = ["https://www.ggzy.gov.cn/"]
    PROV_CODE = {"110000":"北京","120000":"天津","130000":"河北","140000":"山西","150000":"内蒙古",
        "210000":"辽宁","220000":"吉林","230000":"黑龙江","310000":"上海","320000":"江苏",
        "330000":"浙江","340000":"安徽","350000":"福建","360000":"江西","370000":"山东",
        "410000":"河南","420000":"湖北","430000":"湖南","440000":"广东","450000":"广西",
        "460000":"海南","500000":"重庆","510000":"四川","520000":"贵州","530000":"云南",
        "540000":"西藏","610000":"陕西","620000":"甘肃","630000":"青海","640000":"宁夏",
        "650000":"新疆","710000":"台湾","810000":"香港","820000":"澳门"}
    _RE_LINK = re.compile(r'href=["\'](/information/deal/html/a/(\d{6})/\d{4}/\d{8}/[^"\']+)["\'][^>]*>([^<]{4,80})</a>')

    def _parse_list(self, html, base, region):
        out = []
        seen = set()
        for m in self._RE_LINK.finditer(html):
            href, prov, raw_title = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"\s+", "", raw_title).strip()
            if not title or len(title) < 6:
                continue
            if _is_goods_noise(title) or "使用权" in title or "出让" in title or "有偿使用" in title or "国有土地" in title:
                continue
            url = urllib.parse.urljoin(base, href)
            if url in seen:
                continue
            seen.add(url)
            det_region = self.PROV_CODE.get(prov, "")
            out.append({
                "title": title, "region": det_region, "amount_wan": 0,
                "publish_date": "", "buyer": "", "contact": "", "url": url,
                "type": "tender", "qual": [], "keywords": _kw(title),
            })
        return out

    def fetch(self, region, keyword, pages=2):
        if not LIVE:
            return []
        items = []
        for base in self.LIST_URLS:
            try:
                html = self._get(base)
                time.sleep(DELAY)
                items += self._parse_list(html, base, region)
            except Exception:
                pass
        return items


class ChinaGovPurchaseSource(GenericListSource):
    """中国政府采购网 · 采购公告列表。"""
    LIST_URLS = [
        "https://www.ccgp.gov.cn/cggg/",
    ]


class MockTenderSource(BaseSource):
    """内置示例源：覆盖多领域招标 + 中标两种形态，便于离线验证算法。"""
    kind = "tender"

    def fetch(self, region, keyword, pages=2):
        today = date.today()
        raw = [
            {"title": "阳泉市矿区污水处理厂扩容工程EPC总承包招标公告", "region": "山西阳泉", "amount_wan": 6800,
             "publish_date": (today - timedelta(days=1)).isoformat(), "buyer": "阳泉市住房和城乡建设局",
             "contact": "阳泉市住建局 0353-xxxx", "url": "", "type": "tender",
             "qual": ["市政公用工程施工总承包壹级", "环保工程专业承包壹级"], "keywords": ["市政", "污水处理", "EPC", "环保"]},
            {"title": "吕梁市生活垃圾焚烧发电项目特许经营权招标", "region": "山西吕梁", "amount_wan": 11200,
             "publish_date": (today - timedelta(days=2)).isoformat(), "buyer": "吕梁市城市管理局",
             "contact": "吕梁市城管局 0358-xxxx", "url": "", "type": "tender",
             "qual": ["电力工程施工总承包壹级", "环保工程专业承包壹级"], "keywords": ["垃圾焚烧", "环保", "EPC"]},
            {"title": "晋城市工业园区脱硫脱硝超低排放改造EPC", "region": "山西晋城", "amount_wan": 4500,
             "publish_date": (today - timedelta(days=3)).isoformat(), "buyer": "晋城市生态环境局",
             "contact": "晋城市生态环境局 0356-xxxx", "url": "", "type": "tender",
             "qual": ["环保工程专业承包壹级"], "keywords": ["环保", "脱硫脱硝", "EPC"]},
            {"title": "大同市御河生态修复综合治理工程", "region": "山西大同", "amount_wan": 23000,
             "publish_date": (today - timedelta(days=4)).isoformat(), "buyer": "大同市水务局",
             "contact": "大同市水务局 0352-xxxx", "url": "", "type": "tender",
             "qual": ["水利水电工程施工总承包壹级", "市政公用工程总承包特级"], "keywords": ["生态修复", "市政", "环保"]},
            {"title": "长治市主城区雨污分流改造工程EPC中标候选人公示", "region": "山西长治", "amount_wan": 9800,
             "publish_date": (today - timedelta(days=5)).isoformat(), "buyer": "长治市城市建设工程公司",
             "contact": "", "url": "", "type": "award",
             "qual": ["市政公用工程施工总承包壹级"],
             "keywords": ["市政", "污水处理", "EPC"],
             "winner": "山西建设投资集团有限公司", "winner_amount_wan": 9560,
             "candidates": ["山西建设投资集团有限公司", "太原市政建设集团有限公司", "山西四建集团有限公司"]},
            {"title": "山西省高速公路网加密工程某段施工招标公告", "region": "山西", "amount_wan": 85000,
             "publish_date": (today - timedelta(days=6)).isoformat(), "buyer": "山西省交通运输厅",
             "contact": "山西省交通建设中心 0351-xxxx", "url": "", "type": "tender",
             "qual": ["公路工程施工总承包特级", "桥梁工程专业承包壹级"], "keywords": ["公路", "桥梁", "交通"]},
            {"title": "太原市某三甲医院新院区建设工程EPC总承包招标", "region": "山西太原", "amount_wan": 120000,
             "publish_date": (today - timedelta(days=7)).isoformat(), "buyer": "太原市卫生健康委员会",
             "contact": "太原市卫健委 0351-xxxx", "url": "", "type": "tender",
             "qual": ["建筑工程施工总承包特级", "机电工程施工总承包壹级"], "keywords": ["房建", "建筑", "医院", "EPC", "机电"]},
            {"title": "山西省某抽水蓄能电站引水系统及厂房工程招标", "region": "山西", "amount_wan": 180000,
             "publish_date": (today - timedelta(days=8)).isoformat(), "buyer": "某抽水蓄能有限公司",
             "contact": "", "url": "", "type": "tender",
             "qual": ["水利水电工程施工总承包特级"], "keywords": ["水利", "水电站", "电力"]},
            {"title": "山西某燃煤电厂2×1000MW机组扩建工程EPC中标候选人公示", "region": "山西", "amount_wan": 220000,
             "publish_date": (today - timedelta(days=9)).isoformat(), "buyer": "某能源集团",
             "contact": "", "url": "", "type": "award",
             "qual": ["电力工程施工总承包特级", "机电工程施工总承包壹级"], "keywords": ["电力", "输变电", "机电", "EPC"],
             "winner": "中国能源建设集团山西电力建设有限公司", "winner_amount_wan": 215000,
             "candidates": ["中国能源建设集团山西电力建设有限公司", "山西省工业设备安装集团有限公司", "中国电建集团"]},
            {"title": "山西某露天煤矿智能化升级改造项目招标", "region": "山西", "amount_wan": 95000,
             "publish_date": (today - timedelta(days=10)).isoformat(), "buyer": "某矿业集团",
             "contact": "", "url": "", "type": "tender",
             "qual": ["矿山工程施工总承包特级"], "keywords": ["矿山", "煤矿"]},
            {"title": "山西某经开区标准厂房及安置房建设项目EPC", "region": "山西", "amount_wan": 70000,
             "publish_date": (today - timedelta(days=11)).isoformat(), "buyer": "某经济技术开发区管委会",
             "contact": "", "url": "", "type": "tender",
             "qual": ["建筑工程施工总承包壹级"], "keywords": ["房建", "建筑", "安置房", "EPC"]},
            {"title": "山西某县农光互补光伏电站及配套风电场EPC总承包招标", "region": "山西", "amount_wan": 135000,
             "publish_date": (today - timedelta(days=12)).isoformat(), "buyer": "某新能源开发公司",
             "contact": "", "url": "", "type": "tender",
             "qual": ["电力工程施工总承包特级"], "keywords": ["新能源", "光伏", "风电", "电力", "EPC"]},
            # ---- 历史中标结果（覆盖更多省区） ----
            {"title": "江苏某市轨道交通3号线一期工程施工总承包中标结果", "region": "江苏南京", "amount_wan": 320000,
             "publish_date": (today - timedelta(days=15)).isoformat(), "buyer": "南京市轨道交通建设指挥部",
             "contact": "", "url": "", "type": "award",
             "qual": ["市政公用工程施工总承包特级", "铁路工程施工总承包特级"],
             "keywords": ["交通", "轨道交通", "市政"],
             "winner": "中铁十四局集团有限公司", "winner_amount_wan": 315000,
             "candidates": ["中铁十四局集团有限公司", "中铁十二局集团有限公司", "中铁十七局集团有限公司"]},
            {"title": "河南某高速公路扩建工程XX标段施工中标候选人公示", "region": "河南郑州", "amount_wan": 88000,
             "publish_date": (today - timedelta(days=18)).isoformat(), "buyer": "河南省交通运输厅",
             "contact": "", "url": "", "type": "award",
             "qual": ["公路工程施工总承包特级"],
             "keywords": ["公路", "交通"],
             "winner": "中交第一公路工程局有限公司", "winner_amount_wan": 86500,
             "candidates": ["中交第一公路工程局有限公司", "中铁七局集团有限公司", "河南公路工程局集团有限公司"]},
            {"title": "湖北某水电站大坝及引水隧洞工程中标结果公告", "region": "湖北宜昌", "amount_wan": 156000,
             "publish_date": (today - timedelta(days=22)).isoformat(), "buyer": "湖北能源集团",
             "contact": "", "url": "", "type": "award",
             "qual": ["水利水电工程施工总承包特级"],
             "keywords": ["水利", "水电站", "水库"],
             "winner": "中国水利水电第八工程局有限公司", "winner_amount_wan": 152000,
             "candidates": ["中国水利水电第八工程局", "中国葛洲坝集团", "中国安能建设集团"]},
            {"title": "四川某市第二污水处理厂及配套管网工程EPC中标", "region": "四川成都", "amount_wan": 42000,
             "publish_date": (today - timedelta(days=25)).isoformat(), "buyer": "成都市水务局",
             "contact": "", "url": "", "type": "award",
             "qual": ["市政公用工程施工总承包壹级", "环保工程专业承包壹级"],
             "keywords": ["市政", "污水处理", "环保", "管网", "EPC"],
             "winner": "中国建筑第三工程局有限公司", "winner_amount_wan": 41000,
             "candidates": ["中建三局", "中铁二局集团有限公司", "成都建工集团有限公司"]},
            {"title": "浙江某跨海大桥主桥施工中标结果公告", "region": "浙江宁波", "amount_wan": 210000,
             "publish_date": (today - timedelta(days=30)).isoformat(), "buyer": "浙江省交通投资集团",
             "contact": "", "url": "", "type": "award",
             "qual": ["桥梁工程专业承包壹级", "公路工程施工总承包特级"],
             "keywords": ["桥梁", "公路", "交通"],
             "winner": "中交第二公路工程局有限公司", "winner_amount_wan": 206000,
             "candidates": ["中交二公局", "中铁大桥局集团有限公司", "四川公路桥梁建设集团有限公司"]},
            {"title": "广东某工业园区供热管网及配套热电联产项目中标", "region": "广东广州", "amount_wan": 56000,
             "publish_date": (today - timedelta(days=35)).isoformat(), "buyer": "广州市工业和信息化局",
             "contact": "", "url": "", "type": "award",
             "qual": ["市政公用工程施工总承包壹级", "建筑机电安装工程专业承包壹级"],
             "keywords": ["市政", "供热", "管网", "机电", "安装"],
             "winner": "中国能源建设集团广东火电工程有限公司", "winner_amount_wan": 54500,
             "candidates": ["广东火电工程", "中建安装集团有限公司", "广东省工业设备安装有限公司"]},
            {"title": "山东某港口扩建工程码头及堆场施工中标候选人公示", "region": "山东青岛", "amount_wan": 175000,
             "publish_date": (today - timedelta(days=40)).isoformat(), "buyer": "山东港口集团",
             "contact": "", "url": "", "type": "award",
             "qual": ["港口与航道工程施工总承包特级"],
             "keywords": ["港口", "航道", "水利"],
             "winner": "中交第三航务工程局有限公司", "winner_amount_wan": 171000,
             "candidates": ["中交三航局", "山东港湾建设集团有限公司", "中交第一航务工程局"]},
            {"title": "安徽某三甲医院新院区建设工程EPC总承包中标结果", "region": "安徽合肥", "amount_wan": 125000,
             "publish_date": (today - timedelta(days=45)).isoformat(), "buyer": "安徽省卫生健康委员会",
             "contact": "", "url": "", "type": "award",
             "qual": ["建筑工程施工总承包特级"],
             "keywords": ["建筑", "房建", "医院", "EPC"],
             "winner": "中国建筑第八工程局有限公司", "winner_amount_wan": 122000,
             "candidates": ["中建八局", "安徽建工集团有限公司", "中建三局集团有限公司"]},
        ]
        out = []
        for r in raw:
            if keyword:
                hay = (r.get("title", "") + " " + " ".join(r.get("keywords", [])))
                if keyword not in hay:
                    continue
            out.append(r)
        return out


# ====================== 聚合 + 去重 + 评分 ======================
SOURCES = [ShanxiTenderSource(), GGZYNationalSource(), ChinaGovPurchaseSource(), MockTenderSource()]


def aggregate(region="", keyword="", ttype="all"):
    items = []
    seen = set()
    for src in SOURCES:
        try:
            for it in src.fetch(region, keyword):
                fp = (it.get("title", "") + it.get("publish_date", "") + it.get("url", ""))
                if fp in seen:
                    continue
                seen.add(fp)
                # 无关键词时仅保留命中工程词库的（保证默认视图是全领域工程情报）
                if not keyword and not it.get("keywords"):
                    continue
                if ttype != "all" and it.get("type") != ttype:
                    continue
                items.append(it)
        except Exception as e:
            print("[aggregate] 源异常: %s" % e)

    # 区域筛选：有匹配的 → 只显示匹配区域；无匹配的 → 显示全部（不硬丢）
    if region:
        matching = []
        for it in items:
            reg = it.get("region", "") or ""
            if region in reg or (len(reg) >= 2 and len(region) >= 2 and reg[:2] == region[:2]):
                matching.append(it)
        if matching:
            items = matching

    # 评分排序
    for it in items:
        s = len(it.get("keywords", [])) * 10
        reg = it.get("region", "") or ""
        if region and (region in reg or reg[:2] == region[:2]):
            s += 15
        s += min(float(it.get("amount_wan") or 0) / 1000.0, 20)
        it["score"] = round(s, 1)
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return items


# ---- 内存缓存 + 后台自动刷新（真实时架构核心） ----
_CACHE = {
    "default": {"key": ("山西", "", "all"), "data": None, "ts": 0.0, "ttl": 120.0},  # 默认视图
    "by_query": {},  # 按 (region,keyword,ttype) 分片缓存
}
_CACHE_LOCK = threading.Lock()
_BG_STOP = threading.Event()
_BG_REFRESH_INTERVAL = 60  # 后台每 60 秒自动刷新一次默认视图


def cached_aggregate(region="山西", keyword="", ttype="all"):
    k = (region, keyword, ttype)
    now = time.time()
    with _CACHE_LOCK:
        slot = _CACHE["by_query"].get(k) if k != _CACHE["default"]["key"] else _CACHE["default"]
        if slot and slot["data"] is not None and now - slot["ts"] < slot["ttl"]:
            return slot["data"]
    # 缓存未命中或过期 → 实时抓取
    d = aggregate(region, keyword, ttype)
    with _CACHE_LOCK:
        if k == _CACHE["default"]["key"]:
            _CACHE["default"]["key"], _CACHE["default"]["data"], _CACHE["default"]["ts"] = k, d, now
        else:
            _CACHE["by_query"][k] = {"key": k, "data": d, "ts": now, "ttl": 120.0}
    return d


def _background_refresh_loop():
    """后台线程：定时刷新默认视图，保持数据常新（真实时体验）"""
    last_run = 0.0
    while not _BG_STOP.is_set():
        _BG_STOP.wait(_BG_REFRESH_INTERVAL)
        if _BG_STOP.is_set():
            break
        now = time.time()
        # 上次刷新成功且间隔不足 _BG_REFRESH_INTERVAL 秒 → 跳过
        if last_run > 0 and now - last_run < _BG_REFRESH_INTERVAL - 5:
            continue
        try:
            region, keyword, ttype = _CACHE["default"]["key"]
            with _CACHE_LOCK:
                slot = _CACHE["default"]
                # 缓存还没过期跳过（上次用户请求刚刷新过）
                if slot["data"] is not None and now - slot["ts"] < slot["ttl"] * 0.8:
                    continue
            print("[后台刷新] 开始抓取全领域默认视图...")
            d = aggregate(region, keyword, ttype)
            with _CACHE_LOCK:
                _CACHE["default"]["data"], _CACHE["default"]["ts"] = d, now
            last_run = now
            print("[后台刷新] 完成，%d 条工程信息" % len(d))
        except Exception as e:
            print("[后台刷新] 失败: %s，下次重试" % e)


def start_background_refresh():
    """启动后台自刷新线程（仅 LIVE 模式才有意义）"""
    if not LIVE:
        print("[后台刷新] 当前为示例模式，跳过后台自刷新")
        return
    t = threading.Thread(target=_background_refresh_loop, daemon=True, name="bg-refresh")
    t.start()
    print("[后台刷新] 已启动，每 %d 秒自动刷新" % _BG_REFRESH_INTERVAL)


def stop_background_refresh():
    _BG_STOP.set()


# ====================== 竞标单位 / 体量推断 ======================
def scale_advice(amount_wan):
    a = float(amount_wan or 0)
    if a >= 20000:
        return {"level": "大型", "qual_level": "特级 / 壹级总承包", "note": "建议对接大型央企/省属建工集团"}
    if a >= 5000:
        return {"level": "中型", "qual_level": "壹级总承包 / 专业承包", "note": "省属建工及大型专业公司可承接"}
    if a >= 1000:
        return {"level": "中小型", "qual_level": "贰级以上", "note": "地市级骨干企业合适"}
    return {"level": "小型", "qual_level": "叁级即可", "note": "本地中小施工企业可参与"}


def candidates_for(tender, top_n=6):
    region = tender.get("region", "")
    need_qual = tender.get("qual", []) or []
    kws = set(tender.get("keywords", []))
    amount = float(tender.get("amount_wan") or 0)
    scored = []
    for e in ENTERPRISES:
        s = 0
        reasons = []
        if region and region[:2] and region[:2] in e["region"]:
            s += 20; reasons.append("本省企业")
        hit_qual = [q for q in need_qual if any(q in eq or eq in q for eq in e["qual"])]
        s += len(hit_qual) * 10
        if hit_qual:
            reasons.append("资质匹配%d项" % len(hit_qual))
        hist_hit = kws & set(e["history"])
        s += len(hist_hit) * 8
        if hist_hit:
            reasons.append("历史做过%s" % "/".join(hist_hit))
        cap = float(e["reg_capital"])
        if amount > 0:
            if cap >= amount / 3:
                s += 10; reasons.append("体量适配")
            elif cap >= amount / 10:
                s += 4
            else:
                s -= 6; reasons.append("体量偏小")
        scored.append({
            "id": e["id"], "name": e["name"], "region": e["region"],
            "reg_capital_wan": e["reg_capital"], "employees": e["employees"],
            "qual": e["qual"], "score": s, "reasons": reasons,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {
        "tender": {"title": tender.get("title"), "region": region, "amount_wan": amount,
                   "buyer": tender.get("buyer", ""), "qual": need_qual, "keywords": list(kws)},
        "scale": scale_advice(amount),
        "candidates": scored[:top_n],
        "winner": tender.get("winner"),
        "candidates_award": tender.get("candidates", []),
    }


# ====================== 请求分发（FC / 本地共用） ======================
_STATUS_TEXT = {200: "OK", 204: "No Content", 404: "Not Found", 500: "Internal Server Error"}

def serve_file_bytes(fname):
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None

def dispatch(method, path, query):
    """返回 (status_int, content_type, body_bytes)，FC 与本地共用同一套业务逻辑"""
    if method == "OPTIONS":
        return 204, "text/plain", b""
    if path in ("/", "/index.html"):
        # FC Custom Runtime 强制加 Content-Disposition:attachment，HTML 无法内联渲染
        # 重定向到 CloudStudio 静态托管（无 attachment 问题）
        redirect_to = "https://6ef49dbd24f949cca24c069c9cf79997.bj6.agentos-app.net"
        html = f'<html><meta http-equiv="refresh" content="0;url={redirect_to}"><body>正在跳转到首页…<br><a href="{redirect_to}">点击这里</a></body></html>'.encode("utf-8")
        return 200, "text/html; charset=utf-8", html
    if path == "/api/health":
        cache_age = int(time.time() - _CACHE["default"]["ts"]) if _CACHE["default"]["data"] else -1
        obj = {
            "status": "ok", "live": LIVE,
            "sources": [s.__class__.__name__ for s in SOURCES],
            "cache_age_sec": cache_age,
            "bg_refresh_interval_sec": _BG_REFRESH_INTERVAL if LIVE else 0,
            "deploy": "aliyun-fc",
        }
        return 200, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if path == "/api/tenders":
        region = query.get("region", ["山西"])[0]
        keyword = query.get("keyword", [""])[0]
        ttype = query.get("type", ["all"])[0]
        return 200, "application/json; charset=utf-8", json.dumps(cached_aggregate(region, keyword, ttype), ensure_ascii=False).encode("utf-8")
    if path == "/api/candidates":
        tid = query.get("tender_id", [""])[0]
        items = aggregate(query.get("region", ["山西"])[0], query.get("keyword", [""])[0])
        try:
            idx = int(tid)
            tender = items[idx]
        except Exception:
            tender = items[0] if items else {}
        return 200, "application/json; charset=utf-8", json.dumps(candidates_for(tender), ensure_ascii=False).encode("utf-8")
    if path == "/api/enterprises":
        region = query.get("region", [""])[0]
        data = ENTERPRISES
        if region:
            data = [e for e in data if region[:2] in e["region"]]
        return 200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8")
    return 404, "application/json; charset=utf-8", json.dumps(
        {"msg": "not found", "hint": "可用 /api/tenders /api/candidates /api/enterprises"}, ensure_ascii=False).encode("utf-8")

def wsgi_handler(environ, start_response):
    """阿里云 FC · Python 运行时 HTTP 触发器入口（WSGI 风格，零依赖）"""
    try:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO") or "/"
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        if method == "OPTIONS":
            start_response("204 No Content", [
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type"),
            ])
            return [b""]
        status, ctype, body = dispatch(method, path, qs)
        start_response("%d %s" % (status, _STATUS_TEXT.get(status, "OK")), [
            ("Content-Type", ctype),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Content-Length", str(len(body))),
        ])
        return [body]
    except Exception as e:
        import traceback as _tb
        error_body = json.dumps({"error": str(e), "trace": _tb.format_exc()}, ensure_ascii=False).encode("utf-8")
        start_response("500 Internal Server Error", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Access-Control-Allow-Origin", "*"),
            ("Content-Length", str(len(error_body))),
        ])
        return [error_body]


# ====================== HTTP API（标准库，本地运行用） ======================
class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, fname, ctype):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            return self._send({"msg": "前端文件 %s 缺失" % fname}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # 手动写响应（绕过 http.server 自动小写化，避免 FC 误加 Content-Disposition:attachment）
        resp = (
            "HTTP/1.1 204 No Content\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "\r\n"
        ).encode("ascii")
        self.wfile.write(resp)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        status, ctype, body = dispatch("GET", parsed.path, qs)
        status_text = {200: "OK", 204: "No Content", 404: "Not Found", 500: "Error"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("ascii")
        self.wfile.write(head + body)

    def log_message(self, *a):
        pass


def run():
    start_background_refresh()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    try:
        with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
            print("居间小助手后端已启动 → http://0.0.0.0:%d  (LIVE=%s, PORT=%d)" % (PORT, LIVE, PORT))
            print("  接口: /api/tenders  /api/candidates  /api/enterprises  /api/health")
            print("  前端: http://0.0.0.0:%d/  (同源托管 index.html)" % PORT)
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_background_refresh()


if __name__ == "__main__":
    run()
