"""
内容清洗与智能标注模块 (Content Analyzer)
视角：腾讯云国际站（Tencent Cloud International）开发者社媒运营
目标：把热点梳理回可执行的社媒动作，而不是只给一层泛标签

分析方式：
- 标签/评分/产品匹配：本地规则引擎（不调用 LLM）
- 英文→中文翻译：Google Translate API（整句翻译，不做关键词拼装）
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Sequence, Tuple

# ---------- 翻译引擎 ----------
_translator = None

def _get_translator():
    """懒加载 Google Translate 实例"""
    global _translator
    if _translator is None:
        try:
            from googletrans import Translator
            _translator = Translator()
        except ImportError:
            logging.warning("googletrans 未安装，将回退到规则引擎翻译")
            _translator = False  # 标记为不可用
    return _translator

def _google_translate(text: str, max_len: int = 500) -> str:
    """调用 Google Translate 把英文翻译成中文。
    
    失败时静默返回空字符串（由调用方决定兜底策略）。
    """
    if not text or not text.strip():
        return ""
    translator = _get_translator()
    if not translator:
        return ""
    try:
        # 截断过长文本避免 API 超时
        clean = text.strip()[:max_len]
        result = translator.translate(clean, dest="zh-cn")
        return (result.text or "").strip()
    except Exception as e:
        logging.debug(f"Google Translate 调用失败: {e}")
        return ""

# ============================================================
# 1. 通用工具
# ============================================================


def _dedupe_keep_order(values: Sequence[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result



def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())



def _truncate(text: str, limit: int = 180) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"



def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))



def _keyword_hits(text: str, keywords: Sequence[str]) -> List[str]:
    hits: List[str] = []
    normalized = text.lower()
    for keyword in keywords:
        raw = keyword.strip()
        if not raw:
            continue
        lowered = raw.lower()
        if _has_cjk(lowered) or " " in lowered or "/" in lowered or "-" in lowered or "." in lowered:
            if lowered in normalized:
                hits.append(raw)
            continue
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])")
        if pattern.search(normalized):
            hits.append(raw)
    return _dedupe_keep_order(hits)



def _match_score(text: str, keywords: Sequence[str]) -> int:
    return len(_keyword_hits(text, keywords))



def _first_sentence(text: str, limit: int = 220) -> str:
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[。！？.!?])\s+|\n+", cleaned)
    for part in parts:
        chunk = _clean_text(part)
        if len(chunk) >= 12:
            return _truncate(chunk, limit)
    return _truncate(cleaned, limit)


# ============================================================
# 2. 关键词字典
# ============================================================

CLOUD_KEYWORDS = [
    "云计算", "cloud", "aws", "azure", "gcp", "阿里云", "腾讯云", "华为云",
    "tencent cloud", "alibaba cloud", "huawei cloud", "serverless", "kubernetes",
    "k8s", "docker", "容器", "微服务", "saas", "paas", "iaas", "云原生",
    "cloud native", "cloudflare", "cdn", "edge", "对象存储", "oss", "cos", "s3",
    "负载均衡", "弹性伸缩", "vercel", "netlify", "render", "fly.io", "railway",
    "rds", "tdsql", "tidb", "dynamodb", "云数据库", "分布式数据库", "redis",
    "mongodb", "mysql", "postgresql", "waf", "ddos", "ssl", "vpc", "vpn",
    "dns", "live streaming", "转码", "transcoding", "点播", "vod", "rtc", "webrtc",
    "音视频", "trtc", "gpu", "虚拟机", "vm", "vps",
]

AI_KEYWORDS = [
    "ai", "人工智能", "artificial intelligence", "机器学习", "machine learning",
    "深度学习", "deep learning", "大模型", "llm", "large language model",
    "gpt", "chatgpt", "claude", "gemini", "llama", "mistral", "qwen", "deepseek",
    "copilot", "agent", "rag", "transformer", "diffusion", "stable diffusion",
    "midjourney", "sora", "多模态", "multimodal", "nlp", "embedding", "fine-tune",
    "微调", "prompt", "inference", "推理", "训练", "神经网络", "mcp",
    "function calling", "langchain", "llamaindex", "autogen", "crewai", "dify",
    "huggingface", "openai", "anthropic", "google ai", "vector database",
    "knowledge graph", "agentic", "reasoning", "tts", "speech", "ocr",
    "computer vision", "vision model",
]

ANXIETY_KEYWORDS = [
    "焦虑", "裁员", "失业", "35岁", "内卷", "996", "007", "加班",
    "倒闭", "暴雷", "崩盘", "危机", "淘汰", "取代", "替代人类", "恐慌",
    "layoff", "fired", "recession", "bubble", "collapse", "panic", "doom",
]

TOPIC_PATTERNS: List[Tuple[str, List[str]]] = [
    ("技术分享", [
        "教程", "tutorial", "how to", "guide", "实战", "实践", "源码", "source code",
        "开源", "open source", "框架", "framework", "库", "library", "工具", "tool",
        "sdk", "api", "代码", "code", "编程", "programming", "开发", "development",
        "架构", "architecture", "benchmark", "性能", "优化",
    ]),
    ("行业动态", [
        "融资", "收购", "ipo", "上市", "发布", "release", "launch", "announced", "宣布",
        "合作", "partnership", "投资", "investment", "market", "市场", "增长", "growth",
        "战略", "strategy", "进军", "扩展", "pricing", "price", "定价", "降价",
    ]),
    ("观点评论", [
        "评论", "观点", "opinion", "看法", "analysis", "分析", "评测", "review",
        "对比", "comparison", "vs", "预测", "prediction", "展望", "趋势", "trend",
        "争议", "controversy", "讨论", "discussion", "debate", "洞察", "insight",
    ]),
    ("部署教程", [
        "部署", "deploy", "deployment", "安装", "install", "配置", "configuration",
        "setup", "搭建", "运维", "devops", "ci/cd", "pipeline", "terraform", "ansible",
        "helm", "docker compose", "yaml", "nginx", "监控", "monitoring", "日志", "logging",
    ]),
    ("产品发布", [
        "新品", "new product", "product launch", "product hunt", "beta", "alpha",
        "preview", "early access", "更新", "update", "升级", "upgrade", "new version",
    ]),
    ("学术研究", [
        "论文", "paper", "research", "study", "arxiv", "学术", "academic", "实验",
        "experiment", "数据集", "dataset", "sota", "preprint", "conference", "neurips",
        "iclr", "cvpr", "acl", "emnlp",
    ]),
]

PRODUCT_TAGS = {
    # —— 腾讯云产品 ——
    "腾讯云": ["腾讯云", "tencent cloud"],
    "EdgeOne": ["edgeone", "edge one", "边缘安全加速平台"],
    "Lighthouse": ["lighthouse", "轻量应用服务器", "tencent cloud lighthouse"],
    "COS": ["腾讯云cos", "cloud object storage", "tencent cos"],
    "TDSQL-C": ["tdsql", "tdsql-c", "云原生数据库"],
    "CDN": ["tencent cdn", "腾讯云cdn"],
    "IM": ["腾讯云im", "即时通信im", "tencent im"],
    "TRTC": ["trtc", "腾讯实时音视频"],
    "SMS": ["腾讯云短信"],
    "SES": ["邮件推送", "tencent ses"],
    "ES": ["elasticsearch service", "腾讯云es"],
    "CBS": ["云硬盘", "cloud block storage"],
    "Hunyuan3D": ["hunyuan3d", "混元3d", "腾讯混元生3d"],
    "CodeBuddy": ["codebuddy"],
    "云直播": ["腾讯云直播", "tencent cloud live"],
    # —— 知名 AI 产品/模型（用于产品标签） ——
    "ChatGPT": ["chatgpt"],
    "Claude": ["claude"],
    "Gemini": ["gemini"],
    "GPT-4": ["gpt-4", "gpt-4o", "gpt-4.1"],
    "DeepSeek": ["deepseek"],
    "Llama": ["llama"],
    "Mistral": ["mistral"],
    "通义千问": ["通义千问", "qwen"],
    "Copilot": ["github copilot", "copilot"],
    "Cursor": ["cursor"],
    "Dify": ["dify"],
    "LangChain": ["langchain"],
    "HuggingFace": ["huggingface", "hugging face"],
}

TECH_TAGS = {
    "LLM": ["llm", "大模型", "large language model"],
    "RAG": ["rag", "retrieval augmented"],
    "Agent": ["agent", "agentic"],
    "MCP": ["mcp", "model context protocol"],
    "Transformer": ["transformer", "attention mechanism"],
    "Diffusion": ["diffusion", "stable diffusion", "sdxl"],
    "Fine-tuning": ["fine-tune", "微调", "lora", "qlora", "peft"],
    "Vector DB": ["向量数据库", "vector database", "pinecone", "milvus", "weaviate", "chromadb"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Serverless": ["serverless", "函数计算", "cloud function"],
    "3D Generation": ["3d", "text to 3d", "image to 3d", "3d asset", "3d model", "mesh"],
    "WebAssembly": ["wasm", "webassembly"],
    "Rust": ["rust", "cargo"],
    "Go": ["golang", " go ", "golang"],
    "Python": ["python", "pytorch", "tensorflow"],
    "TypeScript": ["typescript", " ts ", "deno", "bun"],
    "React": ["react", "next.js", "nextjs"],
    "Vue": ["vue", "nuxt"],
}

COMPETITOR_TAGS = {
    # —— 海内外云厂商竞品 ——
    "AWS": ["aws", "amazon web services", "amazon ec2", "amazon s3", "amazon eks", "aws lambda", "amazon bedrock"],
    "Azure": ["azure", "microsoft azure", "azure openai", "azure functions", "aks"],
    "GCP": ["gcp", "google cloud", "vertex ai", "cloud run", "gke"],
    "阿里云": ["阿里云", "alicloud", "aliyun", "阿里巴巴云"],
    "华为云": ["华为云", "huawei cloud"],
    "火山引擎": ["火山引擎", "volcengine", "字节云"],
    "百度智能云": ["百度智能云", "百度云", "baidu cloud"],
    "Cloudflare": ["cloudflare", "cloudflare workers", "cloudflare r2"],
    "Vercel": ["vercel"],
    "Netlify": ["netlify"],
    "DigitalOcean": ["digitalocean", "digital ocean"],
    "Render": ["render.com"],
    "Railway": ["railway.app"],
    "Fly.io": ["fly.io"],
}

COMPETITOR_PRODUCT_TAGS = {
    # —— 云厂商的具体竞品产品/服务 ——
    "Amazon Bedrock": ["amazon bedrock", "aws bedrock"],
    "AWS Lambda": ["aws lambda"],
    "Amazon EC2": ["amazon ec2", "ec2 instance"],
    "Amazon S3": ["amazon s3"],
    "Azure OpenAI": ["azure openai"],
    "Azure Functions": ["azure functions"],
    "Google Vertex AI": ["vertex ai"],
    "Cloud Run": ["cloud run"],
    "Cloudflare Workers": ["cloudflare workers"],
    "Cloudflare Pages": ["cloudflare pages"],
    "Cloudflare R2": ["cloudflare r2"],
    "Vercel": ["vercel"],
    "Netlify": ["netlify"],
}

CONTENT_TONE_PATTERNS: List[Tuple[str, List[str]]] = [
    ("热点事件", ["热点", "刷屏", "viral", "breaking", "突发", "重磅", "刚刚", "爆红"]),
    ("对比测评", ["对比", "测评", "评测", "vs", "comparison", "benchmark", "横评", "实测"]),
    ("正向推广", ["推荐", "好用", "awesome", "amazing", "best", "top", "impressive"]),
    ("负面拉踩", ["差评", "垃圾", "不行", "坑", "吐槽", "bug", "漏洞", "安全问题", "price war"]),
    ("中立资讯", ["报告", "report", "数据", "statistics", "调查", "survey", "whitepaper"]),
]

TCLOUD_PRODUCT_RULES: List[Dict[str, Any]] = [
    # ====== 基于应用场景做关联 ======
    # 核心思路：不是"文章提到了产品名才关联"，而是基于潜在应用场景。
    # 例如一个 Agent 开源项目，用户可能会部署到 Lighthouse 上 7x24 运行，那就关联 Lighthouse。

    # -- Lighthouse 轻量应用服务器 --
    # 场景：明确需要自托管部署的工具/服务（不是"凡是开源就推 Lighthouse"）
    # 关键词要求：项目内容本身要涉及"部署"或"自托管"的明确信号
    {
        "short": "Lighthouse",
        "english_name": "Lighthouse (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/lighthouse",
        "description": "轻松部署应用程序，简化各类项目的设置流程。开箱即用环境，AI 镜像支持。",
        "angle_cn": "这个项目/工具可以用 Lighthouse 轻量服务器一键部署，让普通开发者也能快速跑起来",
        "angle_en": "This project can be deployed on Lighthouse for one-click setup, making it accessible to everyday developers.",
        "keywords": [
            "lighthouse", "轻量应用服务器", "轻量服务器",
            # 明确的自部署信号（不要放 agent/bot/open source 等太宽泛的词）
            "self-hosted", "self hosted", "self-hosting",
            "docker compose", "docker-compose", "dockerfile",
            "one-click deploy", "one click deploy", "quick deploy",
            "always online", "7x24", "24/7", "vps",
            # 明确的 Bot 托管场景（要求完整短语，不是单独的 "bot"）
            "discord bot", "slack bot", "telegram bot", "whatsapp bot",
            "n8n", "自部署", "私有部署", "私有化部署",
        ],
        "explicit": ["lighthouse", "tencent cloud lighthouse", "轻量应用服务器"],
        "min_hits": 2,
        "score_weight": 3.2,
    },
    # -- EdgeOne 边缘安全加速平台 --
    # 场景：网站/API/静态站点/Web 应用的加速与防护；前端部署
    {
        "short": "EdgeOne",
        "english_name": "EdgeOne (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/edgeone",
        "description": "全球领先的 CDN、安全防护、边缘计算解决方案。边缘 Pages 自动化部署前端应用。",
        "angle_cn": "这个项目的网站/API/前端可以用 EdgeOne 做全球加速和安全防护，或用 Edge Pages 部署",
        "angle_en": "Use EdgeOne for global acceleration, security protection, or deploy via Edge Pages.",
        "keywords": [
            "edgeone", "边缘安全加速",
            # 应用场景关键词：网站、前端、API
            "cdn", "edge", "waf", "ddos", "l4", "l7",
            "web security", "security", "global acceleration",
            "static site", "web app", "frontend deploy",
            "edge function", "edge computing",
            "latency", "tls", "ssl",
        ],
        "explicit": ["edgeone", "tencent edgeone", "边缘安全加速平台", "edge pages"],
        "min_hits": 2,
        "score_weight": 3.0,
    },
    # -- CodeBuddy AI 智能编码 --
    # 场景：AI 编码工具、代码助手、IDE AI 插件（不是泛泛的"开发工具"）
    {
        "short": "CodeBuddy",
        "english_name": "CodeBuddy (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/codebuddy",
        "description": "AI 深度融入从需求规划、产品设计到代码开发全流程的一站式高效交付平台。",
        "angle_cn": "AI 编码效率提升、从想法到产品发布的一站式开发体验",
        "angle_en": "AI-powered coding productivity — from idea to product launch.",
        "keywords": [
            "codebuddy",
            # AI 编码工具的明确关键词（不放 ide、mcp 等太宽泛的词）
            "ai coding", "coding agent", "code assistant",
            "copilot", "code completion", "code generation",
            "coding tool", "ai code editor",
            # 明确的竞品名
            "cursor", "aider", "cline", "windsurf", "claude code",
            "github copilot", "tabnine", "codeium", "supermaven",
        ],
        "explicit": ["codebuddy", "claude code", "cursor ai"],
        "min_hits": 2,
        "score_weight": 2.8,
    },
    # -- Hunyuan3D 腾讯混元生3D --
    # 场景：3D 内容生成、文本/图片转 3D、游戏/电商/影视制作
    {
        "short": "Hunyuan3D",
        "english_name": "Hunyuan3D (Tencent)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/hunyuan3d",
        "description": "基于腾讯自研生成式AI大模型，支持高质量地生成或处理3D模型。",
        "angle_cn": "文本/图片生成 3D 资产、降低 3D 内容生产门槛",
        "angle_en": "Text/image-to-3D content production and creator efficiency.",
        "keywords": [
            "hunyuan3d", "混元3d",
            # 应用场景关键词：3D 生成（注意不要放 rendering 这种太宽泛的词）
            "text to 3d", "text-to-3d", "image to 3d", "image-to-3d",
            "3d generation", "3d asset", "3d model", "mesh generation",
            "3d printing", "3d content", "3d reconstruction",
            "game asset", "digital content creation",
        ],
        "explicit": ["hunyuan3d", "混元3d", "腾讯混元生3d"],
        "min_hits": 2,
        "score_weight": 2.8,
    },
    # -- COS 对象存储 --
    # 场景：AI 知识库/数据集存储、文件上传、素材管理
    {
        "short": "COS",
        "english_name": "Cloud Object Storage (COS)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/cos",
        "description": "高可用、可靠、可扩展的对象存储。",
        "angle_cn": "AI 工作流中的知识库/数据集/文件/素材可以用 COS 做持久化存储",
        "angle_en": "Object storage for knowledge bases, datasets, files, and AI workflow persistence.",
        "keywords": [
            "cos", "cloud object storage", "对象存储", "tencent cos",
            # 应用场景关键词：存储
            "object storage", "bucket", "storage", "file upload",
            "knowledge base", "dataset", "archive", "asset library",
        ],
        "explicit": ["tencent cos", "腾讯云cos", "cloud object storage"],
        "min_hits": 2,
        "score_weight": 2.2,
    },
    # -- TDSQL-C 云原生数据库 --
    # 场景：需要 MySQL/PostgreSQL 的应用后端
    {
        "short": "TDSQL-C",
        "english_name": "TDSQL-C (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/tdsqlc",
        "description": "企业级云原生数据库，极速性能，海量存储，全面兼容开源数据库。",
        "angle_cn": "需要数据库后端的应用可以用 TDSQL-C 获得云原生 MySQL/PostgreSQL 体验",
        "angle_en": "Cloud-native database compatible with MySQL/PostgreSQL for app backends.",
        "keywords": [
            "tdsql", "云原生数据库", "tdsql-c",
            # 应用场景关键词：数据库
            "database", "mysql", "postgresql", "sql", "rds",
        ],
        "explicit": ["tdsql", "tdsql-c"],
        "min_hits": 2,
        "score_weight": 2.2,
    },
    # -- ES Elasticsearch Service --
    # 场景：搜索、日志分析、RAG 检索
    {
        "short": "ES",
        "english_name": "Elasticsearch Service (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/es",
        "description": "全面管理的人工智能搜索、可观测性和安全服务。",
        "angle_cn": "RAG 检索/日志分析/全文搜索可以用腾讯云 ES 做后端",
        "angle_en": "Managed Elasticsearch for RAG retrieval, log analysis, and full-text search.",
        "keywords": [
            "elasticsearch", "腾讯云es",
            # 应用场景关键词：搜索、RAG
            "search engine", "full text search", "log analysis",
            "rag", "retrieval augmented", "vector search",
            "observability", "logging",
        ],
        "explicit": ["腾讯云es", "tencent elasticsearch", "elasticsearch service"],
        "min_hits": 2,
        "score_weight": 2.2,
    },
    # -- IM 即时通信 --
    # 场景：聊天应用、消息推送、社交功能
    {
        "short": "IM",
        "english_name": "Instant Messaging (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/im",
        "description": "提供全球互通的单聊、群聊、聊天室、系统通知等消息服务。",
        "angle_cn": "聊天/消息/社交功能可以用腾讯云 IM 快速实现",
        "angle_en": "Build chat and messaging features with Tencent Cloud IM.",
        "keywords": [
            "即时通信", "tencent im", "腾讯云im",
            # 应用场景关键词：聊天
            "chat", "messaging", "chat app", "chat sdk", "im sdk",
            "group chat", "chatroom", "real-time messaging",
        ],
        "explicit": ["腾讯云im", "tencent cloud im", "即时通信im"],
        "min_hits": 2,
        "score_weight": 2.0,
    },
    # -- TRTC 实时音视频 --
    # 场景：音视频通话、直播、WebRTC 应用
    {
        "short": "TRTC",
        "english_name": "TRTC (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/trtc",
        "description": "1分钟跑通 Demo，30分钟构建语音通话、视频通话、互动直播。",
        "angle_cn": "音视频通话/互动直播功能可以用 TRTC 快速构建",
        "angle_en": "Build voice/video calls and interactive live streaming with TRTC.",
        "keywords": [
            "trtc", "腾讯实时音视频", "实时音视频",
            # 应用场景关键词：音视频
            "webrtc", "video call", "voice call", "video chat",
            "live streaming", "interactive live", "rtc",
            "audio", "video conference",
        ],
        "explicit": ["trtc", "腾讯实时音视频"],
        "min_hits": 2,
        "score_weight": 2.2,
    },
    # -- 云直播 --
    # 场景：直播推流、视频直播
    {
        "short": "云直播",
        "english_name": "Cloud Live (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/css",
        "description": "低延迟、稳定且易于接入的视频直播服务。亚太视频云市场排名第一。",
        "angle_cn": "视频直播/推流服务可以用腾讯云直播",
        "angle_en": "Low-latency video live streaming service.",
        "keywords": [
            "腾讯云直播", "tencent cloud live", "云直播",
            # 应用场景关键词：直播
            "live stream", "live streaming", "live broadcast",
            "streaming service", "video streaming",
        ],
        "explicit": ["腾讯云直播", "tencent cloud live"],
        "min_hits": 2,
        "score_weight": 2.0,
    },
    # -- CDN 内容分发网络 --
    # 场景：内容加速
    {
        "short": "CDN",
        "english_name": "CDN (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/cdn",
        "description": "快速、稳定、智能、安全的内容加速服务。",
        "angle_cn": "内容分发加速",
        "angle_en": "Fast, stable, intelligent content delivery.",
        "keywords": ["腾讯云cdn", "tencent cdn", "content delivery"],
        "explicit": ["腾讯云cdn", "tencent cdn"],
        "min_hits": 1,
        "score_weight": 1.8,
    },
    # -- CVM 云服务器 --
    # 场景：需要更大算力的部署（GPU 训练、大规模服务等）
    {
        "short": "CVM",
        "english_name": "Cloud Virtual Machine (CVM)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/cvm",
        "description": "安全稳定，高弹性的计算服务。",
        "angle_cn": "需要 GPU/高算力的模型训练和大规模服务可以用 CVM",
        "angle_en": "GPU and high-performance compute for model training and large-scale services.",
        "keywords": [
            "腾讯云服务器", "tencent cvm",
            # 应用场景关键词
            "gpu", "training", "fine-tune", "model training",
            "gpu server", "虚拟机", "vm",
        ],
        "explicit": ["腾讯云服务器", "tencent cvm", "腾讯云cvm"],
        "min_hits": 2,
        "score_weight": 2.0,
    },
    # -- SMS 短信 --
    {
        "short": "SMS",
        "english_name": "SMS (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/sms",
        "description": "快速稳定、简单易用、触达全球的短信服务。",
        "angle_cn": "全球短信触达服务",
        "angle_en": "Global SMS service.",
        "keywords": ["腾讯云短信", "tencent sms", "sms service", "短信验证"],
        "explicit": ["腾讯云短信", "tencent sms"],
        "min_hits": 1,
        "score_weight": 1.5,
    },
    # -- SES 邮件推送 --
    {
        "short": "SES",
        "english_name": "Simple Email Service (Tencent Cloud)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/ses",
        "description": "安全稳定、简单快速、精准高效的邮件推送服务。",
        "angle_cn": "邮件推送服务",
        "angle_en": "Secure and efficient email delivery service.",
        "keywords": ["邮件推送", "tencent ses", "email service", "邮件服务"],
        "explicit": ["腾讯云邮件推送", "tencent ses"],
        "min_hits": 1,
        "score_weight": 1.5,
    },
    # -- CBS 云硬盘 --
    {
        "short": "CBS",
        "english_name": "Cloud Block Storage (CBS)",
        "evidence_type": "scenario",
        "official_anchor": "https://www.tencentcloud.com/products/cbs",
        "description": "持久性数据块存储设备。",
        "angle_cn": "持久性块存储",
        "angle_en": "Persistent block storage.",
        "keywords": ["云硬盘", "tencent cbs", "cloud block storage", "block storage"],
        "explicit": ["腾讯云硬盘", "tencent cbs"],
        "min_hits": 1,
        "score_weight": 1.5,
    },
]

TCLOUD_PRODUCT_NAME_MAP = {rule["short"]: rule["english_name"] for rule in TCLOUD_PRODUCT_RULES}
TCLOUD_RULE_MAP = {rule["short"]: rule for rule in TCLOUD_PRODUCT_RULES}

# ============================================================
# 3. 中文简介生成
# ============================================================

_PROJECT_TYPE_PATTERNS: List[Tuple[str, str]] = [
    (r"(?i)\b(llm|large language model)\b.*?(?:framework|platform|tool)", "{name} — 大语言模型开发框架/平台"),
    (r"(?i)\b(fine.?tun|lora|qlora|peft)\b", "{name} — 大模型微调工具"),
    (r"(?i)\b(rag|retrieval.augmented)\b", "{name} — RAG 检索增强生成框架"),
    (r"(?i)\b(agent|agentic)\b.*?(?:framework|platform|tool|skill)", "{name} — AI Agent 框架/平台"),
    (r"(?i)\b(vector.?(?:db|database)|embedding.?(?:store|search))\b", "{name} — 向量数据库/语义检索工具"),
    (r"(?i)\b(inference|serving|deploy)\b.*?(?:model|llm|ml)", "{name} — 模型推理/部署服务"),
    (r"(?i)\b(code.?(?:gen|assistant|agent|copilot)|coding)\b", "{name} — AI 编程/代码辅助工具"),
    (r"(?i)\b(image|video|diffusion|stable.?diffusion)\b.*?(?:generat|creat|model)", "{name} — AI 图像/视频生成工具"),
    (r"(?i)\b(tts|text.to.speech|speech|voice|asr)\b", "{name} — AI 语音合成/识别工具"),
    (r"(?i)\b(ocr|computer.vision|object.detect|face)\b", "{name} — 计算机视觉/图像识别工具"),
    (r"(?i)\b(kubernetes|k8s|container|docker|helm)\b.*?(?:tool|manag|deploy|platform)", "{name} — 容器/K8s 编排管理工具"),
    (r"(?i)\b(serverless|function|lambda)\b", "{name} — Serverless 无服务器框架"),
    (r"(?i)\b(database|sql|nosql|db)\b.*?(?:tool|manag|platform|new)", "{name} — 数据库管理/工具"),
    (r"(?i)\b(web.?(?:framework|app)|frontend|backend|fullstack)\b", "{name} — Web 开发框架"),
    (r"(?i)\b(cli|command.?line|terminal)\b.*?(?:tool|app)", "{name} — 命令行工具"),
    (r"(?i)\b(productiv|workflow|automat)\b.*?(?:tool|app|platform)", "{name} — 效率/自动化工具"),
    (r"(?i)\b(security|auth|encrypt|privacy)\b.*?(?:tool|framework|lib)", "{name} — 安全/认证工具"),
    (r"(?i)\b(api|gateway|proxy|load.?balanc)\b", "{name} — API 网关/代理服务"),
    (r"(?i)\b(search|crawl|scrap)\b.*?(?:engine|tool|framework)", "{name} — 搜索/爬虫工具"),
    (r"(?i)\b(editor|ide|notebook)\b", "{name} — 编辑器/开发环境"),
    (r"(?i)\b(data.?(?:pipeline|process|etl|lake|warehouse))\b", "{name} — 数据处理/ETL 工具"),
]

_DESC_PHRASE_MAP = [
    (r"(?i)make(?:s)? it easy to (.+?)(?:\.|,|$)", "让你轻松{0}"),
    (r"(?i)^(?:a |an )?(?:fast|blazing.?fast|high.?perf(?:ormance)?)\s+(.+)", "高性能的{0}"),
    (r"(?i)^(?:a |an )?(?:simple|lightweight|minimal)\s+(.+)", "轻量级的{0}"),
    (r"(?i)^(?:a |an )?(?:modern|next.?gen(?:eration)?)\s+(.+)", "新一代{0}"),
    (r"(?i)^(?:a |an )?open.?source\s+(.+)", "开源的{0}"),
    (r"(?i)^(?:a |an )?self.?hosted?\s+(.+)", "可自部署的{0}"),
    (r"(?i)alternative to (.+)", "{0} 的替代品"),
    (r"(?i)built (?:with|on|for) (.+)", "基于 {0} 构建"),
    (r"(?i)powered by (.+)", "由 {0} 驱动"),
]

_POLISH_MAP = {
    # --- 常见短语（长的放前面优先匹配） ---
    "open source": "开源",
    "open-source": "开源",
    "self-hosted": "可自部署的",
    "self hosted": "可自部署的",
    "ai-powered": "AI 驱动的",
    "ai powered": "AI 驱动的",
    "high-performance": "高性能",
    "high performance": "高性能",
    "real-time": "实时",
    "real time": "实时",
    "cross-platform": "跨平台",
    "privacy-focused": "注重隐私的",
    "large language model": "大语言模型",
    "machine learning": "机器学习",
    "deep learning": "深度学习",
    "computer vision": "计算机视觉",
    "natural language processing": "自然语言处理",
    "fine-tuning": "微调",
    "fine tuning": "微调",
    "vector database": "向量数据库",
    "cloud native": "云原生",
    "cloud-native": "云原生",
    "edge computing": "边缘计算",
    "message queue": "消息队列",
    "command line": "命令行",
    "web ui": "Web 界面",
    "coding agent": "编码 Agent",
    "code assistant": "代码助手",
    "code agent": "代码 Agent",
    "code generation": "代码生成",
    "code completion": "代码补全",
    "code review": "代码审查",
    "code search": "代码搜索",
    "pull request": "Pull Request",
    "software development": "软件开发",
    "software engineering": "软件工程",
    "developer tool": "开发者工具",
    "developer tools": "开发者工具",
    "developer workflow": "开发者工作流",
    "development methodology": "开发方法论",
    "agentic skills framework": "Agent 技能框架",
    "agentic skills": "Agent 技能",
    "agentic workflow": "Agent 工作流",
    "agentic framework": "Agent 框架",
    "agentic ai": "Agent AI",
    "desktop app": "桌面应用",
    "desktop application": "桌面应用",
    "web app": "Web 应用",
    "web application": "Web 应用",
    "available in beta": "已开放 Beta 测试",
    "physics simulation": "物理仿真",
    "simulation engine": "仿真引擎",
    "gpu-accelerated": "GPU 加速的",
    "gpu accelerated": "GPU 加速的",
    "data pipeline": "数据流水线",
    "data processing": "数据处理",
    "batch inference": "批量推理",
    "live streaming": "直播",
    "video streaming": "视频流",
    "object storage": "对象存储",
    "block storage": "块存储",
    "file upload": "文件上传",
    "knowledge base": "知识库",
    "knowledge graph": "知识图谱",
    "that works": "",
    "context usage": "上下文使用情况",
    "active tools": "活跃工具",
    "running agents": "运行中的 Agent",
    "todo progress": "待办进度",
    "what's happening": "运行状态",
    "context window": "上下文窗口",
    "multi-agent": "多 Agent",
    "multi agent": "多 Agent",
    "financial trading": "金融交易",
    "trading framework": "交易框架",
    "api proxy": "API 代理",
    "api gateway": "API 网关",
    "terminal emulator": "终端模拟器",
    "minimum viable": "最小可用的",
    "single board computer": "单板计算机",
    "operating system": "操作系统",
    "situational awareness": "态势感知",
    "safety evaluation": "安全评估",
    "safety evaluations": "安全评估",
    "custom prompts": "自定义提示词",
    "custom prompt": "自定义提示词",
    "unified agent workspace": "统一 Agent 工作区",
    "cloud handoff": "云端无缝切换",
    # --- 动词短语 ---
    "built on": "基于",
    "built upon": "基于",
    "built with": "使用",
    "built for": "面向",
    "powered by": "由…驱动",
    "designed for": "面向",
    "targeting": "面向",
    "specifically targeting": "专门面向",
    "connected to": "连接到",
    "integrated with": "集成了",
    "automate": "自动化",
    "automating": "自动化",
    # --- 名词 ---
    "software": "软件",
    "hardware": "硬件",
    "framework": "框架",
    "library": "库",
    "tool": "工具",
    "toolkit": "工具包",
    "platform": "平台",
    "engine": "引擎",
    "plugin": "插件",
    "plugins": "插件",
    "extension": "扩展",
    "application": "应用",
    "app": "应用",
    "service": "服务",
    "dashboard": "仪表盘",
    "model": "模型",
    "models": "模型",
    "training": "训练",
    "inference": "推理",
    "deployment": "部署",
    "database": "数据库",
    "storage": "存储",
    "monitoring": "监控",
    "automation": "自动化",
    "workflow": "工作流",
    "pipeline": "流水线",
    "container": "容器",
    "cluster": "集群",
    "browser": "浏览器",
    "scraper": "爬虫",
    "parser": "解析器",
    "runtime": "运行时",
    "proxy": "代理",
    "gateway": "网关",
    "scheduler": "调度器",
    "queue": "队列",
    "cache": "缓存",
    "benchmark": "基准测试",
    "generator": "生成器",
    "converter": "转换器",
    "analyzer": "分析器",
    "manager": "管理器",
    "builder": "构建器",
    "viewer": "查看器",
    "writer": "写入器",
    "server": "服务器",
    "client": "客户端",
    "interface": "接口",
    "architecture": "架构",
    "ecosystem": "生态系统",
    "community": "社区",
    "security": "安全",
    "authentication": "认证",
    "authorization": "授权",
    "encryption": "加密",
    "privacy": "隐私",
    "scalability": "可扩展性",
    "reliability": "可靠性",
    "observability": "可观测性",
    "lightweight": "轻量级",
    "serverless": "无服务器",
    "developer": "开发者",
    "developers": "开发者",
    "roboticist": "机器人研究者",
    "roboticists": "机器人研究者",
    "researcher": "研究者",
    "researchers": "研究者",
    "asynchronous": "异步",
    "async": "异步",
    "synchronous": "同步",
    "accelerator": "加速器",
    "accelerators": "加速器",
    "receipt": "收据",
    "receipts": "收据",
    "invoice": "发票",
    "invoices": "发票",
    "transaction": "交易",
    "transactions": "交易",
    "accounting": "记账",
    "categories": "分类",
    "category": "分类",
    "configuration": "配置",
    "accessibility": "可访问性",
    "available": "可用",
    "performance": "性能",
    "methodology": "方法论",
    "paradigm": "范式",
    "paradigms": "范式",
    "newsletter": "行业通讯",
    "newsletters": "行业通讯",
    "forecast": "预测",
    "trillion": "万亿",
    "billion": "十亿",
    "infrastructure": "基础设施",
    "superpower": "超能力",
    "superpowers": "超能力",
    "evaluation": "评估",
    "evaluations": "评估",
    "guide": "指南",
    "compilation": "汇编",
    "essay": "文章",
    "essays": "文章",
    "analysis": "分析",
    "frontier": "前沿",
    "emerging": "新兴",
    "agency": "能动性",
    "tokens": "Token",
    "token": "Token",
    "note": "笔记",
    "notes": "笔记",
    "zero config": "零配置",
    "local-first": "本地优先",
    # --- 常见动词/形容词 ---
    "fast": "快速",
    "faster": "更快",
    "fastest": "最快的",
    "ship": "发布",
    "build": "构建",
    "design": "设计",
    "deploy": "部署",
    "launch": "发布",
    "manage": "管理",
    "create": "创建",
    "integrate": "集成",
    "optimize": "优化",
    "exceptional": "出色的",
    "powerful": "强大的",
    "scalable": "可扩展的",
    "secure": "安全的",
    "flexible": "灵活的",
    "simple": "简单的",
    "intuitive": "直觉的",
    "robust": "健壮的",
    "real-time": "实时",
    "seamless": "无缝的",
    "unified": "统一的",
    "captions": "字幕",
    "caption": "字幕",
    "translation": "翻译",
    "anything": "任何东西",
    "flow": "流程",
    "one flow": "一站式流程",
    # --- V12c 补充：修复剩余未翻译词汇 ---
    # 完整短语（放最前面，优先匹配）
    "what makes a good": "怎样写好",
    "a big step for": "的一大进步：",
    "is a big step": "迈出一大步",
    "the fastest way to ship": "最快发布",
    "the fastest way to": "最快的方式",
    "which teaches while you build": "边构建边学习",
    "the world's fastest": "全球最快的",
    "practice tough phone calls with AI before you make them": "用 AI 练习棘手电话",
    "what comes next": "下一步是什么",
    "open models": "开源模型",
    "open model": "开源模型",
    "government control": "政府管控",
    "building the": "打造",
    # 动词
    "shows": "展示",
    "show": "展示",
    "showing": "展示",
    "launches": "启动",
    "makes": "使得",
    "reduce": "降低",
    "loves": "看好",
    "subscribe": "订阅",
    "scanning": "扫描",
    "accelerating": "加速中的",
    "stumbles": "遇挫",
    # 名词
    "simulation": "仿真",
    "simulator": "仿真器",
    "apps": "应用",
    "phone calls": "电话",
    "phone call": "电话",
    "workspace": "工作区",
    "agent workspace": "Agent 工作区",
    "designer": "设计器",
    "instruction": "指令",
    "instructions": "指令",
    "instruction file": "指令文件",
    "instruction files": "指令文件",
    "system prompt": "系统提示词",
    "conversation": "对话",
    "benchmarks": "基准测试",
    "correctness": "正确性",
    "score": "分数",
    "axes": "维度",
    "agents": "Agent",
    "agent": "Agent",
    "episode": "期",
    "email": "邮件",
    "news": "新闻",
    "world": "世界",
    "war": "战争",
    "brain": "大脑",
    "tech": "技术",
    "future": "未来",
    "exposure": "暴露",
    "way": "方式",
    "step": "一步",
    "big step": "一大步",
    "power": "能力",
    "handoff": "切换",
    "content": "内容",
    # 形容词/副词
    "any": "任何",
    "via": "通过",
    "tough": "棘手的",
    "good": "好的",
    "traditional": "传统的",
    "latest": "最新的",
    "pre-loaded": "预加载的",
    "AI-ready": "AI 就绪的",
    "ai-ready data": "AI 就绪数据",
    "changing": "变化的",
    "emerging": "新兴的",
    "embodied": "具身",
    "observed": "观察到的",
    "legged": "多足的",
    "weekly": "每周",
    "daily": "每日",
    "current": "当前",
    "reverse": "逆向",
    "new format": "新格式",
    "PDF parser": "PDF 解析器",
    "cloud handoff": "云端切换",
    "unified agent workspace": "统一 Agent 工作区",
}



def _extract_project_name(item: Dict[str, Any], source_key: str) -> str:
    """提取项目名。对于非项目类来源（v2ex/newsletter/36kr），返回空。"""
    title = item.get("title", "")
    if source_key == "github" and "/" in title:
        name = title.split(" - ", 1)[0].strip()
        if "/" in name:
            name = name.split("/")[-1]
        return name
    if source_key == "producthunt":
        return title.split(" - ", 1)[0].split(" – ", 1)[0].strip()
    if source_key == "huggingface":
        return title.strip()[:60]
    if source_key == "hackernews":
        # HN 标题通常是项目名或文章标题，取 " - " 前的部分
        name = title.split(" - ", 1)[0].split(" – ", 1)[0].strip()[:40]
        return name if name else ""
    # v2ex / 36kr / newsletter 等：这些不是"项目"，返回空
    if source_key in ("v2ex", "36kr", "ai_newsletters"):
        return ""
    return title.split(" - ", 1)[0].strip()[:40]


def _extract_subject_name(item: Dict[str, Any], source_key: str) -> str:
    """提取热点主体名称：项目名或话题简称。
    与 _extract_project_name 不同，这个函数总是返回一个有意义的名称用于描述。
    """
    # 先尝试获取项目名
    project = _extract_project_name(item, source_key)
    if project:
        return project
    # 非项目类来源：从标题中提取核心话题短语
    title = item.get("title", "")
    if not title:
        return "该热点"
    # V2EX/36kr/newsletter：从标题中提取有意义的话题描述
    if source_key in ("v2ex", "36kr", "ai_newsletters"):
        return _extract_topic_phrase(title, source_key)
    # 其他来源：截断到合理长度
    return _truncate(title, 20).rstrip("…")


def _extract_topic_phrase(title: str, source_key: str) -> str:
    """从 V2EX/36kr/newsletter 标题中提取核心话题短语。

    比如：
    - "最近 V2 几乎被各种 AI 中转帖子占领了" → "AI 中转"
    - "你真的敢让 AI 自主编写代码吗？" → "AI 自主编码"
    - "ChinAI #351: CAICT launches 2026 AI Safety Evaluations" → "AI 安全评估"
    - "AI to ROI News & Analysis: March 20, 2026" → "AI 行业动态"
    """
    # 中文标题：提取核心话题
    if _has_cjk(title):
        # 去掉标题中的噪音前缀
        cleaned = re.sub(r"^\[.*?\]\s*", "", title)
        cleaned = re.sub(r"^【.*?】\s*", "", cleaned)
        # 提取 AI 相关短语
        ai_topic = re.search(r"(AI\s*\S{1,8})", cleaned)
        if ai_topic:
            phrase = ai_topic.group(1).strip()
            if len(phrase) >= 3:
                return phrase
        # 尝试提取书名号/引号内容
        quoted = re.search(r"[「『《""](.{2,15})[」』》""]", cleaned)
        if quoted:
            return quoted.group(1)
        # 提取第一个有意义的名词短语（中文，2-10字）
        noun_phrase = re.search(r"[\u4e00-\u9fff]{2,10}(?:工具|平台|项目|服务|框架|模型|技术|应用|系统|方案|协议|评估|安全|编码|训练|推理)", cleaned)
        if noun_phrase:
            return noun_phrase.group(0)
        # 截取前 8 个中文字符
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", cleaned)
        if cn_chars:
            combined = "".join(cn_chars)
            return combined[:8] if len(combined) > 8 else combined
        return _truncate(cleaned, 12).rstrip("…")

    # 英文标题（newsletter 常见）
    # 去掉期号前缀 "ChinAI #351:" / "Memia #2026.11:"
    cleaned = re.sub(r"^[\w]+\s*#[\d.]+:\s*", "", title).strip()
    # 去掉日期后缀 "March 20, 2026"
    cleaned = re.sub(r"[,:]?\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    # 去掉 "News & Analysis" 之类的泛化后缀
    cleaned = re.sub(r"\s*(?:News\s*[&]\s*Analysis|Weekly\s+Digest|Round\s*-?\s*Up)\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    # 去掉 emoji 和特殊符号（使用通用 Unicode emoji 范围）
    cleaned = re.sub(r"[\U00010000-\U0010ffff]", " ", cleaned)
    cleaned = re.sub(r"[\u2600-\u27bf\u2300-\u23ff\u2b50-\u2b55\ufe0f\u200d]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if not cleaned:
        return "该行业通讯"

    # 如果还是太长，做关键词翻译后截取
    if len(cleaned) > 40:
        # 提取第一个有意义的短语
        first_phrase = cleaned.split(",")[0].split(":")[0].split(";")[0].strip()
        if len(first_phrase) > 3:
            translated = _polish_english_fragment(first_phrase)
            return _truncate(translated, 40).rstrip("…")

    translated = _polish_english_fragment(cleaned)
    return _truncate(translated, 40).rstrip("…")



def _polish_english_fragment(text: str) -> str:
    result = text
    # 按词条长度降序匹配，确保长短语优先于单词
    sorted_items = sorted(_POLISH_MAP.items(), key=lambda x: len(x[0]), reverse=True)
    for en, cn in sorted_items:
        pattern = re.compile(r"(?<![a-zA-Z])" + re.escape(en) + r"(?![a-zA-Z])", re.IGNORECASE)
        result = pattern.sub(cn, result)
    return _clean_text(result)



def _translate_en_to_cn(text: str, project_name: str = "") -> str:
    """对英文文本做结构化翻译。

    翻译策略：
    1. 预处理：清理 GitHub 前缀，处理 & 和标点
    2. 分句：把长文本按句子拆开，逐句翻译
    3. 对每个句子：先做结构翻译 (_DESC_PHRASE_MAP)，再做关键词翻译 (_POLISH_MAP)
    4. 后处理：清理英文虚词、多余符号
    5. 质量兜底：翻译不好时用项目类型描述代替
    """
    if not text:
        return ""

    # ---- 预处理 ----
    # 清理 GitHub 前缀（"username/repo:" 格式）
    cleaned = re.sub(r"^[\w\-]+/[\w\-]+:\s*", "", text).strip()
    if not cleaned:
        cleaned = text

    # 处理 & 符号：在中文语境中替换为 "和" 或 "与"
    cleaned = re.sub(r"\s*&\s*", " 和 ", cleaned)

    # 第一步：识别项目类型（用于兜底）
    type_cn = ""
    for pattern, template in _PROJECT_TYPE_PATTERNS:
        if re.search(pattern, cleaned):
            type_cn = template.replace("{name}", "").strip(" —— —-")
            break

    # 第二步：分句翻译
    # 按句子拆开，逐句翻译后拼接
    segments = re.split(r"(?<=[.!?])\s+|\s*\.\s+", cleaned)
    translated_segments = []

    for seg in segments:
        seg = seg.strip()
        if not seg or len(seg) < 3:
            continue

        # 尝试整句结构翻译
        seg_translated = seg
        structure_matched = False
        for pattern, template in _DESC_PHRASE_MAP:
            m = re.search(pattern, seg_translated)
            if m:
                captured = _polish_english_fragment(m.group(1).strip()) if m.lastindex else ""
                phrase_cn = template.format(captured) if captured else template.format("")
                seg_translated = seg_translated[:m.start()] + phrase_cn + seg_translated[m.end():]
                structure_matched = True
                break

        # 关键词翻译
        seg_translated = _polish_english_fragment(seg_translated)
        translated_segments.append(seg_translated)

    translated = "。".join(translated_segments) if len(translated_segments) > 1 else (translated_segments[0] if translated_segments else cleaned)

    # 第三步：后处理
    # 计算翻译前的中文占比
    pre_cn_chars = len(re.findall(r"[\u4e00-\u9fff]", translated))
    pre_total = len(re.findall(r"[\u4e00-\u9fffa-zA-Z]", translated))
    pre_cn_ratio = pre_cn_chars / max(pre_total, 1)

    if pre_cn_ratio >= 0.15:
        # 中文占比够了，清理夹杂在中文中的英文虚词
        _STANDALONE_NOISE = [
            r"\bAn?\b", r"\bThe\b", r"\bthat\b", r"\bwhich\b", r"\bwith\b",
            r"\band\b", r"\bor\b", r"\bfor\b", r"\bof\b", r"\bto\b",
            r"\bin\b", r"\bon\b", r"\bis\b", r"\bare\b", r"\bwas\b",
            r"\bit\b", r"\bby\b", r"\bas\b", r"\bfrom\b", r"\binto\b",
            r"\byour\b", r"\byou\b", r"\btheir\b", r"\bits\b", r"\bour\b",
            r"\bthis\b", r"\bthese\b", r"\bNew\b", r"\bvery\b", r"\bmost\b",
            r"\bnow\b", r"\bmore\b", r"\bhere\b", r"\bcan\b", r"\bwill\b",
            r"\bhave\b", r"\bhas\b", r"\bbeen\b", r"\bwho\b", r"\bhow\b",
            r"\bwhat\b", r"\bwhen\b", r"\bwhere\b", r"\bwe\b", r"\bthey\b",
        ]
        # 只在中文占比 >= 30% 时做激进清理
        if pre_cn_ratio >= 0.30:
            for noise in _STANDALONE_NOISE:
                translated = re.sub(noise + r"\s*", "", translated, flags=re.IGNORECASE)
        else:
            # 轻度清理：只清理最常见的冠词和介词
            for noise in [r"\bAn?\b", r"\bThe\b", r"\bof\b", r"\bfor\b", r"\bto\b", r"\bin\b", r"\bon\b"]:
                translated = re.sub(noise + r"\s*", "", translated, flags=re.IGNORECASE)

    # 清理连续标点和多余空格
    translated = re.sub(r"\s*[.!?]+\s*$", "", translated)
    translated = re.sub(r"[.!?]\s*。", "。", translated)  # 避免 ".。" 双标点
    translated = re.sub(r"。{2,}", "。", translated)  # 避免连续句号
    translated = re.sub(r"\s{2,}", " ", translated).strip()
    translated = re.sub(r"^\s*[,，、。]\s*", "", translated)
    translated = re.sub(r"[,，、]\s*$", "", translated)
    # 清理 "和 和" 重复
    translated = re.sub(r"和\s+和", "和", translated)

    # 第四步：检测翻译质量
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", translated))
    total_meaningful = len(re.findall(r"[\u4e00-\u9fffa-zA-Z]", translated))
    cn_ratio = cn_chars / max(total_meaningful, 1)

    if cn_ratio >= 0.3:
        # 翻译效果可以
        if project_name and project_name.lower() not in translated.lower():
            return f"{project_name}：{translated}"
        return translated

    # 第五步：翻译效果不好——构造"项目名 — 类型描述"格式
    if type_cn:
        if project_name:
            return f"{project_name} — {type_cn}"
        return type_cn

    # 兜底：项目名 + 翻译后的文本（即使翻译不完美，也比纯英文好）
    if project_name:
        return f"{project_name}：{translated}"
    return translated


def _extract_en_summary(text: str, source_key: str, item: Dict[str, Any] = None) -> str:
    """从原始英文文本中提取干净的英文摘要（1-2 句话），供翻译 API 使用。"""
    if not text:
        return ""

    # 去掉 GitHub repo 前缀 "username/repo: "
    desc = re.sub(r"^[\w\-]+/[\w\-]+:\s*", "", text).strip()

    # newsletter：优先用标题（detail_brief 通常是长篇正文摘录）
    if source_key == "ai_newsletters":
        title = (item or {}).get("title", "")
        if title:
            # 清理期号前缀和 emoji
            title_clean = re.sub(r"^[\w]+\s*#[\d.]+:\s*", "", title).strip()
            title_clean = re.sub(r"[\U00010000-\U0010ffff]", " ", title_clean)
            title_clean = re.sub(r"\s{2,}", " ", title_clean).strip()
            if title_clean:
                return title_clean

    # ProductHunt："ProductName: tagline" → 取 tagline
    if source_key == "producthunt":
        tagline = re.sub(r"^[\w\s]+:\s*", "", desc, count=1).strip()
        if tagline:
            return tagline

    # 通用：按句拆分，取前 2 句有意义的描述
    sentences = re.split(r"(?<=[.!?])\s+", desc)
    meaningful = []
    for s in sentences:
        s = s.strip()
        if len(s) < 8:
            continue
        if re.match(
            r"^(Download|Sign |Log |Navigation|Menu|Home|Skip|©|"
            r"As always|Please|Greetings from|You signed|Your browser|"
            r"Reload to refresh|All blog posts|curl |npm |pip |brew |"
            r"Free models|Published |Authors? |Tags? |Read more|"
            r"Subscribe|Share this|Click here|Join |Follow )",
            s, re.IGNORECASE,
        ):
            continue
        meaningful.append(s)
        if len(" ".join(meaningful)) > 300:
            break
        if len(meaningful) >= 2:
            break

    return " ".join(meaningful) if meaningful else desc[:300]


def _generate_cn_summary(item: Dict[str, Any], source_key: str, detail_brief: str = "") -> str:
    """中文简介 = 提取英文摘要 → Google Translate 整句翻译。

    流程：
    1. 确定原文文本（detail_brief > summary > description > title）
    2. 如果已经是中文为主，直接返回
    3. 提取干净的 1-2 句英文摘要
    4. 调用 Google Translate API 整句翻译
    5. 翻译失败时回退到规则引擎
    """
    # 确定原文文本
    text = detail_brief or ""
    if not text:
        text = item.get("summary", "") or item.get("description", "") or ""
    if not text:
        text = item.get("title", "")
    if not text:
        return ""

    text = _clean_text(text)

    # 判断是否以中文为主
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cn_chars > len(text) * 0.25:
        return _truncate(text, 200)

    # 第一步：提取干净的英文摘要
    en_summary = _extract_en_summary(text, source_key, item)
    if not en_summary:
        en_summary = text[:300]

    # 第二步：Google Translate 整句翻译
    translated = _google_translate(en_summary)
    if translated and len(re.findall(r"[\u4e00-\u9fff]", translated)) >= 2:
        # 翻译成功，加上项目名前缀（如果翻译结果中没有的话）
        project_name = _extract_project_name(item, source_key)
        if project_name and project_name.lower() not in translated.lower():
            return _truncate(f"{project_name}：{translated}", 200)
        return _truncate(translated, 200)

    # 第三步：翻译 API 失败 → 回退到规则引擎
    project_name = _extract_project_name(item, source_key)
    fallback = _translate_en_to_cn(en_summary, project_name)
    return _truncate(fallback, 200)


# ============================================================
# 4. 具体内容摘要与基础分析
# ============================================================


def _text_for_analysis(item: Dict[str, Any]) -> str:
    """拼接所有文本字段用于关键词匹配。先过噪音清洗，避免页脚/导航中的品牌名污染标签。"""
    parts = [
        item.get("title", ""),
        _strip_web_noise(item.get("summary", "") or ""),
        _strip_web_noise(item.get("content", "") or ""),
        item.get("description", ""),
    ]
    return " ".join(p for p in parts if p).lower()



def _strip_web_noise(text: str) -> str:
    """去除网页爬取中常见的导航栏、页脚、标签等垃圾文本。"""
    if not text:
        return ""

    # ===== 36kr 导航栏噪音（整块清除） =====
    # 匹配 36kr 页面中的固定导航文本块（出现在几乎所有 36kr 页面中）
    _36kr_nav_block = (
        r"(?:数字时氪|未来消费|智能涌现|未来城市|启动Power on|潮生TIDE|"
        r"职场bonus|36碳|后浪研究所|暗涌Waves|硬氪|氪睿研究院|"
        r"媒体品牌|企业号|企服点评|36Kr研究院|36Kr创新咨询|企业服务|核心服务|"
        r"城市之窗|政府服务|创投发布|LP源计划|VClub[^\n]*?投资人服务|"
        r"寻求报道|36氪Pro|创投氪堂|企业入驻|创业者服务|创投平台|AI测评网)"
    )
    # 移除连续的导航栏文本（这些词会连续出现，用\s*连接）
    text = re.sub(_36kr_nav_block + r"[\s]*", " ", text)

    # 移除 36kr 固定块
    text = re.sub(r"AI\s*自助报道\s*广东", " ", text)
    text = re.sub(r"最新\s*创投\s*汽车\s*科技\s*专精特新\s*直播\s*视频\s*专题\s*活动\s*搜索", " ", text)
    text = re.sub(r"我要入驻\s*城市合作", " ", text)
    text = re.sub(r"\d{2}\s*月\s*\d{2}", " ", text)
    text = re.sub(r'分享至\s*打开微信.*?分享按钮', " ", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}", " ", text)
    text = re.sub(r"下一篇\s*.*$", "", text)
    text = re.sub(r"24小时热榜\s*查看更多榜单.*$", "", text)
    text = re.sub(r"36氪获悉[，,]\s*", "", text)
    # 移除 "VClub投资机构库" 等片段
    text = re.sub(r"VClub\s*投资机构库\s*投资机构职位推介\s*投资人认证", " ", text)

    # ===== 36kr 页脚版权噪音（包含"阿里云"会污染标签匹配） =====
    # "本站由 阿里云 提供计算与安全服务" + 后续版权信息
    text = re.sub(r"本站由\s*阿里云\s*提供.*$", "", text)
    # "热门产品 文章标签 快讯标签 合作伙伴 36氪APP下载" 等页脚块
    text = re.sub(r"热门产品\s*文章标签\s*快讯标签.*$", "", text)
    # "36氪APP下载" 后续
    text = re.sub(r"36氪APP下载.*$", "", text)
    # 36kr 底部推荐/广告
    text = re.sub(r"(?:鲸准|氪空间|推送和解读前沿).*$", "", text)
    text = re.sub(r"©\s*\d{4}.*$", "", text)
    # "36氪Auto 数字时氪 未来消费 智能涌现..." 导航头
    text = re.sub(r"36氪Auto\s*数字时氪.*?(?:搜索|AI测评网)", " ", text)

    # ===== 通用网站噪音 =====
    text = re.sub(r"(账号设置|我的关注|我的收藏|申请的报道|退出登录|登录\s*搜索)", " ", text)
    text = re.sub(r"(首页\s*快讯\s*资讯\s*推荐\s*财经)", " ", text)
    text = re.sub(r"Skip to (?:content|main|navigation)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(Facebook|Twitter|LinkedIn|Pinterest|Email|Share|Print)\b(?:\s*\|?\s*)", " ", text)

    # 清理多余空格
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_detail_brief(item: Dict[str, Any], source_key: str = "") -> str:
    """生成一条有实质信息量的内容摘要——读完就知道这条热点到底说了什么。

    策略：
    1. 收集 summary / content / title 中的真实描述性信息
    2. 去掉那些 ≤10 字的无效内容（只有标题重复）
    3. 拼成"标题核心 + 关键描述"的结构
    4. 确保不同于标题本身，否则回退到 content 抽取
    """
    title = _clean_text(item.get("title", ""))
    raw_summary = item.get("summary", "") or item.get("description", "") or ""
    summary = _strip_web_noise(_clean_text(raw_summary))
    raw_content = item.get("content", "") or ""
    content = _strip_web_noise(_clean_text(raw_content))

    # 标题核心（去掉 GitHub 的 org/ 前缀）
    title_core = title
    if source_key == "github" and " - " in title:
        title_core = title.split(" - ", 1)[0].strip()

    # 收集有意义的描述片段（不等于标题本身、且足够长）
    desc_candidates: List[str] = []
    for raw in [summary, content]:
        if not raw:
            continue
        # 跳过和标题几乎相同的内容
        if raw.lower().strip() == title.lower().strip():
            continue
        if raw.lower().strip() == title_core.lower().strip():
            continue
        if len(raw) < 12:
            continue
        desc_candidates.append(raw)

    # 从 title 的 " - " 右侧提取附属描述
    if " - " in title:
        subtitle = title.split(" - ", 1)[1].strip()
        if len(subtitle) >= 10 and subtitle.lower() != summary.lower().strip():
            desc_candidates.insert(0, subtitle)

    # 抽取最佳描述片段：取第一段有意义的 1~3 句话
    best_desc = ""
    for candidate in desc_candidates:
        # 拆成句子，取前 2~3 句有内容的
        sentences = re.split(r"(?<=[。！？.!?])\s*|\n+", candidate)
        meaningful: List[str] = []
        for s in sentences:
            s = _clean_text(s)
            if len(s) < 8:
                continue
            # 跳过明显的导航/模板文本
            if re.match(r"^(首页|登录|搜索|注册|Home|Menu|Navigation|Copyright)", s, re.IGNORECASE):
                continue
            meaningful.append(s)
            if len(" ".join(meaningful)) > 400:
                break
            if len(meaningful) >= 4:
                break
        if meaningful:
            best_desc = " ".join(meaningful)
            break

    if not best_desc:
        # fallback：直接截取 content 前 400 字
        for candidate in desc_candidates:
            best_desc = _truncate(candidate, 400)
            if best_desc:
                break

    # 组装最终摘要
    if best_desc:
        # 如果 best_desc 已经包含标题信息，就直接用
        if title_core.lower() in best_desc.lower() or len(best_desc) > 200:
            return _truncate(best_desc, 500)
        # 否则"标题: 描述"
        return _truncate(f"{title_core}: {best_desc}", 500)

    # 实在没有描述，用中文简介兜底
    cn = _generate_cn_summary(item, source_key)
    if cn:
        return cn
    return _truncate(title, 500)



def score_relevance(item: Dict[str, Any]) -> Dict[str, Any]:
    text = _text_for_analysis(item)
    cloud_hits = _keyword_hits(text, CLOUD_KEYWORDS)
    ai_hits = _keyword_hits(text, AI_KEYWORDS)
    anxiety_hits = _keyword_hits(text, ANXIETY_KEYWORDS)
    return {
        "is_cloud": len(cloud_hits) >= 1,
        "is_ai": len(ai_hits) >= 1,
        "is_anxiety": len(anxiety_hits) >= 1,
        "cloud_score": len(cloud_hits),
        "ai_score": len(ai_hits),
        "anxiety_score": len(anxiety_hits),
        "cloud_hits": cloud_hits[:8],
        "ai_hits": ai_hits[:8],
        "anxiety_hits": anxiety_hits[:6],
    }



def classify_topic(item: Dict[str, Any]) -> str:
    text = _text_for_analysis(item)
    best_topic = "其他"
    best_score = 0
    for topic, keywords in TOPIC_PATTERNS:
        score = _match_score(text, keywords)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic



def extract_tags(item: Dict[str, Any]) -> Dict[str, List[str]]:
    text = _text_for_analysis(item)
    products = [name for name, keywords in PRODUCT_TAGS.items() if _match_score(text, keywords) > 0]
    competitors = [name for name, keywords in COMPETITOR_TAGS.items() if _match_score(text, keywords) > 0]
    competitor_products = [name for name, keywords in COMPETITOR_PRODUCT_TAGS.items() if _match_score(text, keywords) > 0]
    techs = [name for name, keywords in TECH_TAGS.items() if _match_score(text, keywords) > 0]
    return {
        "products": products,
        "competitors": competitors,
        "competitor_products": competitor_products,
        "techs": techs,
    }



def annotate_tone(item: Dict[str, Any]) -> str:
    text = _text_for_analysis(item)
    best_tone = "中立资讯"
    best_score = 0
    for tone, keywords in CONTENT_TONE_PATTERNS:
        score = _match_score(text, keywords)
        if score > best_score:
            best_score = score
            best_tone = tone
    return best_tone



def _parse_heat_number(heat_str: str) -> float:
    if not heat_str:
        return 0.0
    text = str(heat_str).replace(",", "")
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return float(match.group(1)) * 10000
    match = re.search(r"([\d.]+)", text)
    if match:
        return float(match.group(1))
    return 0.0



def _parse_timeliness(time_str: str) -> float:
    if not time_str:
        return 5.0
    text = str(time_str).lower().strip()

    if text in ("real-time", "today", "hot", "just now"):
        return 10.0

    match = re.search(r"(\d+)\s*(hour|小时|h)", text)
    if match:
        hours = int(match.group(1))
        if hours <= 2:
            return 9.0
        if hours <= 6:
            return 8.0
        if hours <= 12:
            return 7.0
        return 6.0

    match = re.search(r"(\d+)\s*(minute|分钟|min)", text)
    if match:
        return 9.5

    match = re.search(r"(\d+)\s*(day|天|d)", text)
    if match:
        days = int(match.group(1))
        return max(1.0, 8.0 - days)

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text[: len(fmt) + 5], fmt)
            delta = datetime.now() - dt
            if delta < timedelta(hours=6):
                return 9.0
            if delta < timedelta(hours=12):
                return 8.0
            if delta < timedelta(days=1):
                return 7.0
            if delta < timedelta(days=2):
                return 5.0
            return max(1.0, 7.0 - delta.days)
        except (ValueError, TypeError):
            continue

    return 5.0


# ============================================================
# 5. 腾讯云国际站产品映射（证据优先）
# ============================================================


def _sort_tcloud_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        matches,
        key=lambda item: (
            0 if item["rule"].get("evidence_type") == "official" else 1,
            -(item.get("score", 0)),
            item["rule"].get("short", ""),
        ),
    )



def _compose_tcloud_storyline(rule: Dict[str, Any], hits: List[str], item: Dict[str, Any] = None, detail_brief: str = "", source_key: str = "") -> str:
    """根据热点具体内容生成差异化的腾讯云结合描述。

    核心原则：
    1. 结合说明必须基于热点的「具体内容」做深度关联，而不是套模板
    2. 要说清楚「为什么这个热点和这个产品有关」，而不只是「可以部署到 XX 上」
    3. 如果关联太弱（只是凑巧命中了宽泛关键词），宁可返回"关联较弱"也不强贴
    """
    product = rule["short"]
    proj = _extract_subject_name(item or {}, source_key) if item else "该项目"

    brief = detail_brief or (item or {}).get("summary", "") or ""
    brief_lower = brief.lower()
    brief_short = _truncate(brief, 120).rstrip("…").strip()

    hit_set = {h.lower() for h in hits}

    # 判断是否为非项目来源（讨论/新闻/通讯）
    is_discussion = source_key in ("v2ex", "36kr", "ai_newsletters")

    if product == "Lighthouse":
        # 只有明确涉及自部署/私有化的才推 Lighthouse
        if hit_set & {"self-hosted", "self hosted", "self-hosting", "私有部署", "自部署", "私有化部署"}:
            if is_discussion:
                return f"「{proj}」讨论到自托管/私有部署需求 — Lighthouse 提供预装 Docker 的轻量服务器，开发者可以 3 分钟内启动实例并通过 Docker Compose 完成部署，数据完全自主可控"
            return f"**{proj}** 明确支持自托管部署（{brief_short[:60]}），Lighthouse 的 Docker 预装镜像可以省去环境配置，开发者通过控制台一键创建实例后直接 docker compose up 即可运行"
        if hit_set & {"docker compose", "docker-compose", "dockerfile"}:
            return f"**{proj}** 提供了 Docker 部署方式，Lighthouse 的 Docker 预装镜像 + 海外节点覆盖（硅谷/法兰克福/新加坡等）适合需要全球访问的自托管场景"
        if hit_set & {"discord bot", "slack bot", "telegram bot", "whatsapp bot"}:
            return f"**{proj}** 是一个需要 7×24 在线的 Bot 服务，Lighthouse 按月付费（最低 $5/月起）、海外节点低延迟，适合长期运行的 Bot 类应用"
        if hit_set & {"n8n"}:
            return f"**{proj}** 涉及工作流自动化（类 n8n），Lighthouse 提供自动化工具一键部署镜像，适合个人开发者低成本搭建自动化流水线"
        # 通用但要有具体内容
        return f"**{proj}** 涉及服务器部署场景，Lighthouse 轻量应用服务器提供海外多区域节点和开箱即用环境，适合中小型项目的快速上线"

    if product == "EdgeOne":
        if hit_set & {"waf", "ddos", "security", "web security"}:
            if is_discussion:
                return f"「{proj}」讨论到 Web 安全防护 — EdgeOne 整合了 L3/L4/L7 DDoS 防护 + 智能 WAF + Bot 管理，可以作为话题中安全方案的具体产品推荐"
            return f"**{proj}** 涉及安全扫描/防护场景，EdgeOne 提供 L3/L4/L7 全链路 DDoS 防护和智能 WAF 规则，可以在安全话题中做能力展示"
        if hit_set & {"cdn", "latency", "global acceleration"}:
            return f"**{proj}** 面向全球用户，EdgeOne 在全球 3,200+ 节点提供 CDN 加速，并整合安全防护，适合需要低延迟 + 安全的场景"
        if hit_set & {"static site", "frontend deploy", "edge function"}:
            return f"**{proj}** 涉及前端/静态站点，EdgeOne Pages 支持 Git 自动部署 + 全球 CDN + 边缘函数，类似 Vercel/Netlify 但自带安全防护"
        return f"**{proj}** 的网站/API 可以用 EdgeOne 做全球加速（3,200+ 节点）和安全防护（DDoS + WAF + Bot 管理）"

    if product == "CodeBuddy":
        if is_discussion:
            if hit_set & {"coding agent", "code agent", "ai coding", "copilot", "cursor"}:
                return f"「{proj}」讨论 AI 编程工具 — CodeBuddy 的差异化在于：全流程覆盖（需求→设计→编码→测试）+ IDE 原生集成 + 中文优化，可以在讨论中做对比推荐"
            return f"「{proj}」涉及开发者效率话题 — CodeBuddy 提供从需求规划到代码发布的一站式 AI 编码体验，可以作为开发效率提升的具体工具推荐"
        if hit_set & {"coding agent", "code agent", "ai coding"}:
            return f"**{proj}** 是 AI 编程赛道产品 — 可以做 CodeBuddy vs {proj} 的横向对比：CodeBuddy 覆盖需求→设计→编码→测试全流程，IDE 原生集成而非浏览器插件"
        if hit_set & {"copilot", "cursor", "aider", "cline"}:
            return f"**{proj}** 的用户群和 CodeBuddy 高度重合 — 可以写「{proj} 用户迁移到 CodeBuddy 的体验对比」，突出 CodeBuddy 的多文件编辑和 Agent 模式"
        if hit_set & {"code completion", "code generation"}:
            return f"**{proj}** 涉及代码生成能力 — CodeBuddy 在代码补全之外还提供 Agent 模式（自动拆解需求→多文件编辑→运行测试），可以做能力升级的对比"
        return f"**{proj}** 涉及 AI 编码场景，CodeBuddy 提供 IDE 集成的全流程 AI 编码能力（需求→设计→编码→测试），可以做横向对比或互补推荐"

    if product == "Hunyuan3D":
        if hit_set & {"text to 3d", "text-to-3d", "image to 3d", "image-to-3d"}:
            return f"**{proj}** 涉及文本/图片转 3D — Hunyuan3D 是腾讯自研的 3D 生成大模型，支持文本/图片输入直接生成高质量 3D 模型，可以做效果对比展示"
        if hit_set & {"3d model", "3d asset", "mesh generation", "game asset"}:
            return f"**{proj}** 产出的 3D 资产可以用 Hunyuan3D 辅助生成 — Hunyuan3D 支持自动生成 mesh 和纹理，降低 3D 内容生产的人力成本"
        return f"**{proj}** 涉及 3D 内容创作 — Hunyuan3D 支持文本/图片到 3D 的自动生成，可以在 3D 话题中做腾讯技术实力的展示"

    if product == "COS":
        if "knowledge base" in brief_lower or "知识库" in brief_lower or "rag" in brief_lower:
            return f"**{proj}** 涉及知识库/RAG 场景 — COS 对象存储可以作为知识库文档的持久化存储后端，配合 CDN 加速全球访问"
        if "dataset" in brief_lower or "数据集" in brief_lower:
            return f"**{proj}** 涉及数据集管理 — COS 对象存储支持 EB 级数据存储 + 生命周期管理 + 全球加速分发，适合大规模数据集的存取场景"
        return f"**{proj}** 产生的文件/数据可以用 COS 对象存储做持久化存储，COS 提供 99.9999999999% 的数据可靠性 + 全球加速分发"

    if product == "TDSQL-C":
        if "mysql" in brief_lower:
            return f"**{proj}** 使用 MySQL 数据库 — TDSQL-C 提供 100% 兼容 MySQL 的云原生数据库，支持自动扩缩容和按量计费，省去自建 MySQL 的运维成本"
        if "postgresql" in brief_lower or "postgres" in brief_lower:
            return f"**{proj}** 使用 PostgreSQL — TDSQL-C 提供 100% 兼容 PostgreSQL 的云原生数据库，支持向量扩展（pgvector）用于 AI 场景"
        return f"**{proj}** 需要数据库后端 — TDSQL-C 云原生数据库兼容 MySQL/PostgreSQL，支持秒级扩缩容 + Serverless 按量付费，适合从个人项目到企业级的弹性场景"

    if product == "ES":
        if hit_set & {"rag", "retrieval augmented", "vector search"}:
            return f"**{proj}** 涉及 RAG 检索 — 腾讯云 ES 支持向量搜索 + BM25 混合检索，可以作为 RAG 应用的检索后端，提供开箱即用的语义搜索能力"
        if "log" in brief_lower or "observability" in brief_lower:
            return f"**{proj}** 涉及日志/可观测性 — 腾讯云 ES 提供日志分析 + APM + SIEM 能力，可以作为可观测性基础设施"
        return f"**{proj}** 涉及搜索场景 — 腾讯云 ES 提供全文搜索 + 向量搜索 + 日志分析的一站式能力"

    if product == "IM":
        if "chat" in brief_lower or "messaging" in brief_lower:
            return f"**{proj}** 涉及即时通讯功能 — 腾讯云 IM 提供单聊/群聊/聊天室/消息推送全套 SDK，30 分钟内即可集成到现有应用"
        return f"**{proj}** 涉及消息/聊天场景 — 腾讯云 IM 提供全球互通的即时通讯能力，SDK 覆盖 iOS/Android/Web/Flutter 等平台"

    if product == "TRTC":
        if "webrtc" in brief_lower or "video call" in brief_lower:
            return f"**{proj}** 涉及实时音视频通话 — TRTC 提供低延迟（<300ms）的全球音视频通话能力，SDK 支持 Web/iOS/Android/Flutter"
        return f"**{proj}** 涉及音视频场景 — TRTC 提供实时音视频通话 + 互动直播能力，1 分钟跑通 Demo、30 分钟上线"

    if product == "CVM":
        if hit_set & {"gpu", "training", "fine-tune", "model training"}:
            return f"**{proj}** 涉及模型训练/GPU 计算 — CVM 提供 NVIDIA A100/H100 GPU 实例，支持弹性扩缩容，按需使用避免硬件采购成本"
        return f"**{proj}** 需要大规模计算资源 — CVM 弹性云服务器提供从通用型到 GPU 型的多种规格，支持按需扩容"

    # 通用兜底：不强贴产品
    if is_discussion:
        return f"「{proj}」话题与 {product} 存在场景关联，但关联度中等，建议结合具体讨论内容判断是否植入"
    return f"**{proj}** 与 {product} 存在潜在场景关联，建议人工判断关联强度后再决定是否做内容"



def evaluate_tcloud_integration(item: Dict[str, Any], relevance: Dict[str, Any], detail_brief: str = "", source_key: str = "") -> Dict[str, Any]:
    text = _text_for_analysis(item)
    matches: List[Dict[str, Any]] = []
    evidence_keywords: List[str] = []

    # 非项目来源（新闻通讯/36kr 资讯）需要更高的命中门槛
    # 因为这些来源提到 "agent"/"deploy" 是很正常的，但不意味着它本身是可部署工具
    non_project_sources = {"ai_newsletters", "36kr"}
    min_hits_boost = 2 if source_key in non_project_sources else 0

    for rule in TCLOUD_PRODUCT_RULES:
        explicit_hits = _keyword_hits(text, rule.get("explicit", []))
        keyword_hits = _keyword_hits(text, rule.get("keywords", []))
        all_hits = _dedupe_keep_order(explicit_hits + keyword_hits)
        hit_count = len(all_hits)
        required_hits = rule.get("min_hits", 1) + min_hits_boost

        if explicit_hits or hit_count >= required_hits:
            score = rule.get("score_weight", 1.0) + min(hit_count * 0.25, 1.2)
            matches.append({
                "rule": rule,
                "hits": all_hits,
                "score": score,
            })
            evidence_keywords.extend(all_hits[:5])

    matches = _sort_tcloud_matches(matches)
    matched_products = [m["rule"]["short"] for m in matches]
    reasons = [_compose_tcloud_storyline(m["rule"], m["hits"], item, detail_brief, source_key) for m in matches[:3]]

    official_matches = [m for m in matches if m["rule"].get("evidence_type") in ("official", "scenario")]
    priority_matches = [m for m in matches if m["rule"].get("evidence_type") == "priority"]
    primary_match = matches[0] if matches else None

    base = sum(m.get("score", 0) for m in matches[:2])
    if official_matches:
        base += 1.2
    if relevance.get("is_ai"):
        base += 0.5
    if relevance.get("is_cloud"):
        base += 0.7
    if relevance.get("cloud_score", 0) >= 3:
        base += 0.4
    if relevance.get("is_anxiety"):
        base -= 1.6
    if not matches:
        base -= 1.0

    score = max(0.0, min(10.0, base))

    if primary_match:
        primary_rule = primary_match["rule"]
        relation_type = "应用场景"
        integration_text = _compose_tcloud_storyline(primary_rule, primary_match["hits"], item, detail_brief, source_key)
        if len(matches) > 1:
            secondary_rule = matches[1]["rule"]
            secondary_text = _compose_tcloud_storyline(secondary_rule, matches[1]["hits"], item, detail_brief, source_key)
            integration_text += f"\n补充关联 {secondary_rule['short']}：{secondary_text}"
        official_storyline = (
            f"如果以腾讯云国际站官方号来写，这条建议主推 {primary_rule['short']}：{integration_text}"
        )
    else:
        relation_type = "待人工判断"
        integration_text = "暂未命中足够强的国际站产品锚点，不建议强贴产品，先作为行业观察保留。"
        official_storyline = "暂未匹配到明确的腾讯云产品，可保留为行业观察。"

    return {
        "tcloud_relevance": round(score, 1),
        "tcloud_integration": integration_text,
        "tcloud_products": matched_products,
        "tcloud_reasons": reasons,
        "tcloud_evidence": _dedupe_keep_order(evidence_keywords)[:10],
        "tcloud_primary_product": primary_match["rule"]["short"] if primary_match else "",
        "tcloud_relation_type": relation_type,
        "tcloud_official_storyline": official_storyline,
        "tcloud_official_matches": [item["rule"]["short"] for item in official_matches],
        "tcloud_priority_matches": [item["rule"]["short"] for item in priority_matches],
    }


# ============================================================
# 6. 综合评分
# ============================================================


def compute_composite_score(
    heat_str: str,
    time_str: str,
    relevance: Dict[str, Any],
    tcloud: Dict[str, Any],
) -> Dict[str, float]:
    heat_num = _parse_heat_number(heat_str)
    timeliness = _parse_timeliness(time_str)

    if heat_num >= 50000:
        heat_score = 10.0
    elif heat_num >= 10000:
        heat_score = 9.0
    elif heat_num >= 5000:
        heat_score = 8.0
    elif heat_num >= 1000:
        heat_score = 7.0
    elif heat_num >= 500:
        heat_score = 6.0
    elif heat_num >= 100:
        heat_score = 5.0
    elif heat_num >= 50:
        heat_score = 4.0
    elif heat_num >= 10:
        heat_score = 3.0
    elif heat_num > 0:
        heat_score = 2.0
    else:
        heat_score = 1.0

    ai_score = min(relevance.get("ai_score", 0) * 2, 10)
    cloud_score = min(relevance.get("cloud_score", 0) * 2, 10)
    ai_cloud_score = min((ai_score + cloud_score) / 2 + 2, 10) if (ai_score > 0 or cloud_score > 0) else 0
    tcloud_relevance = tcloud.get("tcloud_relevance", 0)

    composite = (
        tcloud_relevance * 0.35
        + ai_cloud_score * 0.25
        + heat_score * 0.20
        + timeliness * 0.20
    )

    if relevance.get("is_anxiety"):
        composite *= 0.65

    return {
        "heat_score": round(heat_score, 1),
        "timeliness_score": round(timeliness, 1),
        "ai_cloud_score": round(ai_cloud_score, 1),
        "composite_score": round(composite, 1),
    }


# ============================================================
# 7. 反推社媒动作
# ============================================================


def _derive_topic_type(
    source_key: str,
    topic: str,
    tone: str,
    competitors: List[str],
    competitor_products: List[str],
    techs: List[str],
) -> str:
    if competitor_products or (topic == "行业动态" and competitors):
        return "竞品动态"
    if topic in {"技术分享", "部署教程"} or source_key in {"github", "v2ex"}:
        return "开发者话题"
    if topic == "产品发布":
        return "产品动态"
    if topic == "学术研究":
        return "研究前沿"
    if tone == "热点事件":
        return "社区热议"
    if techs:
        return "开发者话题"
    return "行业观察"



def _derive_editorial_angles(
    topic_type: str,
    topic: str,
    tone: str,
    tcloud_products: List[str],
    competitor_products: List[str],
    techs: List[str],
    source_key: str,
) -> List[str]:
    angles: List[str] = []

    if topic_type == "开发者话题":
        angles.append("技术科普")
    if topic in {"部署教程", "技术分享"} or any(tag in techs for tag in ["Kubernetes", "Serverless", "RAG", "MCP"]):
        angles.append("教程拆解")
    if tcloud_products:
        angles.append(f"产品关联（{' / '.join(tcloud_products[:2])}）")
    if competitor_products:
        angles.append(f"竞品对比（{' / '.join(competitor_products[:2])}）")
    elif topic_type == "竞品动态":
        angles.append("竞品对比")
    if topic in {"观点评论", "行业动态"} or tone in {"热点事件", "对比测评", "负面拉踩"}:
        angles.append("观点评论")
    if topic == "学术研究":
        angles.append("研究解读")
    if source_key in {"hackernews", "v2ex"}:
        angles.append("社区参与")

    if not angles:
        angles.append("信息快讯")
    return _dedupe_keep_order(angles)[:4]



def _derive_publish_priority(topic_type: str, timeliness_score: float, composite_score: float) -> str:
    if topic_type == "竞品动态" and timeliness_score >= 7.0:
        return "P0 今天发"
    if topic_type in {"社区热议", "产品动态"} and timeliness_score >= 8.0:
        return "P0 今天发"
    if composite_score >= 8.0:
        return "P0 今天发"
    if composite_score >= 6.5 and timeliness_score >= 7.0:
        return "P1 本周发"
    if topic_type == "开发者话题" and composite_score >= 6.0:
        return "P1 本周发"
    return "P2 观察中"



def _derive_platforms(topic_type: str, source_key: str) -> List[str]:
    if topic_type == "竞品动态":
        return ["X（快评）", "LinkedIn（深度分析）"]
    if topic_type == "开发者话题":
        platforms = ["Reddit（参与讨论）", "LinkedIn（教程文章）"]
        if source_key in {"github", "hackernews", "v2ex"}:
            platforms.append("Discord（社区互动）")
        return platforms
    if topic_type == "研究前沿":
        return ["LinkedIn（观点总结）", "X（快讯）"]
    if topic_type == "社区热议":
        return ["X（快评）", "Reddit（参与讨论）"]
    if topic_type == "产品动态":
        return ["X（上新快讯）", "LinkedIn（产品解读）"]
    return ["LinkedIn（行业观察）", "X（摘要快讯）"]



def _product_display_name(product: str) -> str:
    if not product:
        return ""
    return {
        "CloudBase": "云开发 / CloudBase",
        "Hunyuan3D": "混元3D / Hunyuan3D",
    }.get(product, product)



def _fit_platform_text(text: str, limit: int) -> str:
    return _truncate(text, limit)



def _angles_to_en(angles_text: str) -> str:
    result = angles_text or "quick update"
    replacements = {
        "技术科普": "technical explainer",
        "教程拆解": "hands-on walkthrough",
        "社区参与": "community conversation",
        "观点评论": "opinionated commentary",
        "研究解读": "research takeaway",
        "信息快讯": "quick update",
    }
    for cn, en in replacements.items():
        result = result.replace(cn, en)
    result = re.sub(r"产品关联（([^）]+)）", r"product tie-in (\1)", result)
    result = re.sub(r"竞品对比（([^）]+)）", r"competitor comparison (\1)", result)
    result = result.replace(" + ", " + ")
    return result



def _build_visual_brief(primary_product: str, detail_brief: str, item: Dict[str, Any] = None, source_key: str = "") -> str:
    """根据热点具体内容生成差异化的配图建议。
    
    原则：配图要和热点内容本身相关，而不是千篇一律的产品截图拼接。
    """
    proj = _extract_subject_name(item or {}, source_key) if item else "该项目"
    title = (item or {}).get("title", "") or ""

    brief_lower = (detail_brief or "").lower()
    is_discussion = source_key in ("v2ex", "36kr", "ai_newsletters")

    # 优先根据热点内容类型决定配图方向
    if source_key == "github":
        # GitHub 项目：用项目本身的 README 截图或 Demo GIF
        if "3d" in brief_lower or "3d" in title.lower():
            return f"主视觉用 {proj} 的 3D 效果展示图或 Demo 截图，展示技术效果比文字更有说服力。"
        if "security" in brief_lower or "vulnerability" in brief_lower or "scan" in brief_lower:
            return f"主视觉用 {proj} 的扫描结果/漏洞报告截图，展示安全工具的实际输出效果。"
        if any(kw in brief_lower for kw in ["pdf", "document", "parser"]):
            return f"主视觉用 {proj} 的文档解析效果对比图（原始 PDF vs 结构化输出），直观展示工具价值。"
        if any(kw in brief_lower for kw in ["video", "automate", "money"]):
            return f"主视觉用 {proj} 的工作流程图或产出效果截图，展示自动化流程的关键步骤。"
        return f"主视觉用 {proj} 的 GitHub README 中的效果截图/架构图/Demo GIF，展示项目的核心功能。"

    if source_key == "hackernews":
        if "blog" in brief_lower or "essay" in brief_lower or "opinion" in brief_lower or "thoughts" in brief_lower:
            return f"主视觉用文章核心观点的金句图（白底大字），配合作者头像和文章标题，适合 LinkedIn/X 传播。"
        if "tool" in brief_lower or "emulator" in brief_lower or "framework" in brief_lower:
            return f"主视觉用工具/框架的架构图或使用流程截图，如果有命令行输出可以截取关键操作步骤。"
        return f"主视觉围绕文章核心论点做信息图或金句图，避免使用通用插画。"

    if source_key == "v2ex":
        return f"主视觉用项目的产品截图或使用效果展示，如果是讨论帖则提取核心观点做金句图。"

    if source_key == "producthunt":
        return f"主视觉直接用 {proj} 的 Product Hunt 首图或产品 Demo 截图，展示产品界面和核心功能。"

    if source_key == "huggingface":
        return f"主视觉用论文的核心图表（Figure 1 或方法框架图），标注关键创新点，适合技术受众。"

    if is_discussion:
        return f"主视觉用话题核心观点的金句图 + 数据可视化（如果有数据），避免使用通用的产品截图拼接。"

    # 兜底
    return f"主视觉围绕 {proj} 的核心功能/效果做截图或信息图，优先展示实际效果而非概念拼图。"



def build_writing_pack(
    item: Dict[str, Any],
    detail_brief: str,
    topic_type: str,
    angles_text: str,
    tcloud: Dict[str, Any],
    source_key: str = "",
) -> Dict[str, Any]:
    primary_product = tcloud.get("tcloud_primary_product") or ""
    product_cn = _product_display_name(primary_product)
    product_cn_for_copy = product_cn or "人工判断场景"
    product_en = TCLOUD_PRODUCT_NAME_MAP.get(primary_product, primary_product or "Tencent Cloud scenario")
    primary_rule = TCLOUD_RULE_MAP.get(primary_product, {})
    storyline = tcloud.get("tcloud_official_storyline") or tcloud.get("tcloud_integration") or "先不强贴产品，保留行业观察。"
    storyline_en = primary_rule.get("angle_en") or "Keep it as an industry signal and avoid forcing a product pitch before there is stronger evidence."
    relation_type = tcloud.get("tcloud_relation_type", "待人工判断")
    brief_cn = _truncate(detail_brief or item.get("summary_cn") or item.get("title", "这条热点"), 68)
    headline_en = _truncate(item.get("title", detail_brief or "This trend"), 88)
    angles_en_text = _angles_to_en(angles_text)
    visual_brief = _build_visual_brief(primary_product, detail_brief, item, source_key)

    if primary_product:
        proj = _extract_subject_name(item, source_key)
        is_discussion = source_key in ("v2ex", "36kr", "ai_newsletters")
        integration_text = tcloud.get("tcloud_integration", "")
        brief_short = _truncate(detail_brief or "", 80)

        # 根据热点具体内容 + 产品 + 来源类型生成差异化写作建议
        # 核心原则：写作角度要紧扣热点内容，而不是千篇一律的"写一篇XX教程"
        if is_discussion:
            if primary_product == "CodeBuddy":
                official_angle_cn = f"借「{proj}」的讨论热度写一篇 AI 编程工具选型指南，从社区关注的痛点（{brief_short}）出发，自然引出 CodeBuddy 在全流程覆盖和 IDE 集成上的差异化。"
            elif primary_product == "Lighthouse":
                official_angle_cn = f"以「{proj}」话题中的开发者痛点为引子，写一篇实用的自托管部署指南，用 Lighthouse 演示从零到上线的完整流程。"
            elif primary_product == "EdgeOne":
                official_angle_cn = f"从「{proj}」话题中的性能/安全需求出发，写一篇场景化的解决方案文章，展示 EdgeOne 如何解决这类实际问题。"
            else:
                official_angle_cn = f"以「{proj}」话题为引子，从社区讨论中提炼出一个具体的技术痛点，用 {product_cn} 的能力来给出解决方案。"
        else:
            # 项目类来源：根据热点的具体功能决定写作角度
            if primary_product == "CodeBuddy":
                official_angle_cn = f"以 {proj} 引发的开发者效率讨论为切入点，写一篇 CodeBuddy 的实际使用案例：用 CodeBuddy 的 Agent 模式从零构建一个类似功能，展示 AI 编码的真实生产力。"
            elif primary_product == "Lighthouse" and any(k in (integration_text or "").lower() for k in ["自部署", "docker", "self-host"]):
                official_angle_cn = f"以「5 分钟在 Lighthouse 上部署 {proj}」为主题写一篇实操教程，从创建实例到服务上线，附完整命令和截图，让读者可以直接复制操作。"
            elif primary_product == "Lighthouse":
                official_angle_cn = f"围绕 {proj} 的使用场景写一篇开发者实践文章，重点展示在 Lighthouse 上搭建和运行的完整过程，附性能数据和成本对比。"
            elif primary_product == "EdgeOne":
                official_angle_cn = f"从 {proj} 的性能/安全需求出发，写一篇「如何用 EdgeOne 给你的项目加速和防护」的实践指南，附真实的延迟对比数据。"
            elif primary_product == "Hunyuan3D":
                official_angle_cn = f"以 {proj} 引发的 3D 内容创作话题为切入点，写一篇 Hunyuan3D 的效果展示和技术解读，用实际生成结果说话。"
            elif primary_product == "TDSQL-C":
                official_angle_cn = f"以 {proj} 的数据库需求为切入，写一篇从自建数据库迁移到 TDSQL-C 的实操指南，重点对比运维成本和性能提升。"
            elif primary_product == "ES":
                official_angle_cn = f"从 {proj} 的搜索/检索场景出发，写一篇用腾讯云 ES 构建智能搜索的技术内容，附完整的索引和查询示例。"
            elif primary_product == "CVM":
                official_angle_cn = f"以 {proj} 的算力需求为切入，写一篇在 CVM GPU 实例上跑模型训练/推理的实操内容，附硬件规格和成本估算。"
            else:
                official_angle_cn = f"围绕 {proj} 的核心功能（{brief_short}），以 {product_cn} 为切入点写实操内容，重点展示产品如何解决热点中的具体问题。"
        official_angle_en = f"Use {product_en} as the official hook and turn the trend into a product-led developer story instead of a news repost."
    else:
        official_angle_cn = "这条暂不建议硬贴具体产品，可先保留为行业观察，再做人工二次判断。"
        official_angle_en = "Do not force a product angle yet. Keep it as an industry signal and review manually."

    x_cn = _fit_platform_text(
        f"{brief_cn}。如果站在腾讯云国际站官方号来写，这条更适合从 {product_cn_for_copy} 切入：{storyline}",
        134,
    )
    x_en = _fit_platform_text(
        f"{headline_en}. For Tencent Cloud, this fits {product_en}: {storyline_en}",
        250,
    )
    discord_cn = _fit_platform_text(
        f"今天看到一条值得开发者关注的热点：{brief_cn}。如果从腾讯云视角切，这条最适合带到 {product_cn_for_copy}，重点聊 {angles_text}。你更关注部署、性能还是工作流？",
        220,
    )
    discord_en = _fit_platform_text(
        f"A developer trend worth discussing today: {headline_en}. From a Tencent Cloud angle, the best hook is {product_en}, especially around {angles_en_text}. What matters more to you: deployment, performance, or workflow?",
        360,
    )
    linkedin_cn = _fit_platform_text(
        f"这条热点表面上是资讯，真正值得写的是它和腾讯云国际站产品的连接。{brief_cn}。如果用官方号发，建议主推 {product_cn_for_copy}，重点讲 {angles_text}，并把内容拉回 {storyline}",
        420,
    )
    linkedin_en = _fit_platform_text(
        f"This trend is more useful as a product-led developer story than a news repost. {headline_en}. For Tencent Cloud, the better angle is {product_en}. {storyline_en} The content should focus on {angles_en_text}.",
        700,
    )
    reddit_title_cn = _fit_platform_text(f"这类热点如果从 {product_cn_for_copy} 切，会更值得聊吗？", 70)
    reddit_body_cn = _fit_platform_text(
        f"看到这条热点：{brief_cn}。如果站在腾讯云国际站官方号视角，我会更想把它写成 {product_cn_for_copy} 相关内容，重点是 {angles_text}。你觉得这种切法会更有信息量吗？",
        320,
    )
    reddit_title_en = _fit_platform_text(f"Would this trend be more useful if framed through {product_en}?", 90)
    reddit_body_en = _fit_platform_text(
        f"A recent trend caught my eye: {headline_en}. Instead of reposting it as news, I'd frame it through {product_en} and focus on {angles_en_text}. Would that make the discussion more useful for developers?",
        520,
    )

    summary = f"已生成 X / Discord / LinkedIn / Reddit 的中英文文案，主推 {product_cn or '人工判断'}。"

    return {
        "primary_product": primary_product,
        "product_display": product_cn,
        "relation_type": relation_type,
        "official_angle_cn": official_angle_cn,
        "official_angle_en": official_angle_en,
        "visual_brief": visual_brief,
        "summary": summary,
        "x_cn": x_cn,
        "x_en": x_en,
        "discord_cn": discord_cn,
        "discord_en": discord_en,
        "linkedin_cn": linkedin_cn,
        "linkedin_en": linkedin_en,
        "reddit_title_cn": reddit_title_cn,
        "reddit_body_cn": reddit_body_cn,
        "reddit_title_en": reddit_title_en,
        "reddit_body_en": reddit_body_en,
    }



def _derive_priority_reason(topic_type: str, timeliness_score: float, composite_score: float) -> str:
    """给优先级加上可执行的时效说明"""
    if topic_type == "竞品动态" and timeliness_score >= 7.0:
        return "P0 今天发（竞品动态时效性强）"
    if topic_type in {"社区热议", "产品动态"} and timeliness_score >= 8.0:
        return "P0 今天发（话题正在活跃期）"
    if composite_score >= 7.0:
        return "P0 今天发（综合分数高，值得抢先发）"
    if timeliness_score >= 8.0:
        return "P1 本周发（讨论还在活跃期）"
    if topic_type == "开发者话题":
        return "P1 本周发（开发者话题持续性强）"
    if timeliness_score >= 6.0:
        return "P1 本周发（仍有时效价值）"
    return "P2 观察中（可择机再发）"


def _derive_platform_reason(topic_type: str, source_key: str) -> List[str]:
    """给平台推荐加上具体的动作理由"""
    if topic_type == "竞品动态":
        return ["X（快评竞品动态）", "LinkedIn（发深度对比分析文章）"]
    if topic_type == "开发者话题":
        platforms = ["Reddit（直接参与讨论）", "LinkedIn（发教程文章）"]
        if source_key in {"github", "hackernews", "v2ex"}:
            platforms.append("Discord（开发者社区互动）")
        return platforms
    if topic_type == "研究前沿":
        return ["LinkedIn（发观点总结文章）", "X（论文快讯分享）"]
    if topic_type == "社区热议":
        return ["X（快评蹭热度）", "Reddit（参与社区讨论）"]
    if topic_type == "产品动态":
        return ["X（产品上新快讯）", "LinkedIn（产品功能深度解读）"]
    return ["LinkedIn（行业观察文章）", "X（摘要快讯分享）"]


def _build_angle_detail(
    item: Dict[str, Any],
    topic_type: str,
    angles: List[str],
    detail_brief: str,
    tcloud: Dict[str, Any],
    competitor_products: List[str],
    techs: List[str],
) -> str:
    """构建可用角度的详细说明——不是标签堆砌，而是具体的运营方向。

    关键原则：
    - 说清楚这个热点具体讲了什么（不只是标题缩写）
    - 给出具体可执行的写作方向（不是"教程拆解"这种空洞标签）
    - 如果和腾讯云产品有关联，说清楚为什么有关
    """
    title = _truncate(item.get("title", ""), 50)
    primary_product = tcloud.get("tcloud_primary_product") or ""
    product_display = _product_display_name(primary_product) or ""
    integration_text = tcloud.get("tcloud_integration", "")

    parts: List[str] = []

    # 第一段：热点核心内容 + 技术方向
    tech_str = " / ".join(techs[:3]) if techs else ""
    brief_short = _truncate(detail_brief or "", 60)
    if tech_str and brief_short:
        parts.append(f"「{title}」（{brief_short}），涉及 {tech_str}")
    elif tech_str:
        parts.append(f"「{title}」涉及 {tech_str} 技术方向")
    elif brief_short:
        parts.append(f"「{title}」（{brief_short}）")
    else:
        parts.append(f"「{title}」")

    # 第二段：基于热点内容的具体写作方向
    if topic_type == "竞品动态" and competitor_products:
        comp_str = " / ".join(competitor_products[:2])
        parts.append(f"可写方向：{comp_str} 的动态对腾讯云意味着什么，以及差异化应对策略")
    elif primary_product and integration_text:
        # 取结合说明的核心要点作为写作方向
        integration_short = _truncate(integration_text.replace(f"**{_extract_subject_name(item, '')}**", "").strip(), 80)
        if integration_short:
            parts.append(f"可写方向：{integration_short}")
    elif topic_type == "开发者话题":
        if any(tag in techs for tag in ["Kubernetes", "Serverless", "MCP", "RAG"]):
            tag = next(tag for tag in techs if tag in ["Kubernetes", "Serverless", "MCP", "RAG"])
            parts.append(f"可写方向：{tag} 技术实践教程 + 腾讯云产品实操演示")
        else:
            parts.append(f"可写方向：技术解读 + 开发者社区讨论参与")
    elif topic_type == "社区热议":
        parts.append("可写方向：带腾讯云视角参与社区讨论，输出差异化观点")

    return " → ".join(parts) if parts else " + ".join(angles)


def _build_social_recommendation(
    item: Dict[str, Any],
    source_key: str,
    topic_type: str,
    primary_product: str,
    product_display: str,
    tcloud: Dict[str, Any],
    angles_text: str,
    priority: str,
    priority_reason: str,
    platforms_text: str,
) -> str:
    """根据热点的实际内容生成差异化的社媒结论，而不是固定模板。

    关键原则：
    - 每条热点的结论应该读起来像是针对这条热点专门写的
    - 结论要包含具体的行动方向（做什么类型的内容），而不只是"围绕XX做内容"
    - 不同来源（GitHub/HN/V2EX/PH/Newsletter）的切入方式天然不同
    """
    title = _truncate(item.get("title", ""), 40)
    proj = _extract_subject_name(item, source_key)
    storyline = tcloud.get("tcloud_official_storyline") or tcloud.get("tcloud_integration") or ""
    official_angle = ""
    # 从 tcloud 中提取写作角度关键信息
    integration_text = tcloud.get("tcloud_integration", "")

    is_discussion = source_key in ("v2ex", "36kr", "ai_newsletters")

    if not primary_product:
        # 无产品关联：根据来源和内容给不同建议
        if topic_type == "竞品动态":
            return (
                f"「{title}」是竞品动态，建议以行业观察视角切入，"
                f"可写竞品对比或趋势点评。{priority_reason}，推荐平台：{platforms_text}。"
            )
        if is_discussion:
            return (
                f"「{title}」是社区热议话题，暂未关联具体产品。"
                f"可以以腾讯云开发者视角参与讨论，或作为行业洞察保留。推荐平台：{platforms_text}。"
            )
        return (
            f"「{title}」暂未命中明确产品关联。"
            f"可先作为行业观察素材，人工判断是否适合关联腾讯云产品。推荐 {platforms_text}。"
        )

    # 有产品关联 → 根据产品 + 内容类型 + 来源类型生成差异化结论

    # --- CodeBuddy 相关 ---
    if primary_product == "CodeBuddy":
        if "竞品" in integration_text or "对比" in integration_text:
            return (
                f"「{title}」是 CodeBuddy 的同赛道竞品，适合做横向对比评测内容，"
                f"突出 IDE 集成和中文支持的差异化优势。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if is_discussion:
            return (
                f"「{title}」讨论 AI 编程工具话题，可以自然切入 CodeBuddy 做产品推荐。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        return (
            f"借「{title}」的热度带出 CodeBuddy，内容方向：AI 编程工具选型科普或使用心得分享。"
            f"{priority_reason}，推荐平台：{platforms_text}。"
        )

    # --- Lighthouse 相关 ---
    if primary_product == "Lighthouse":
        if "自部署" in integration_text or "docker" in integration_text.lower():
            if is_discussion:
                return (
                    f"「{title}」话题涉及自部署需求，切入 Lighthouse 一键部署能力做产品种草。"
                    f"内容形式：观点文章或经验分享。{priority_reason}，推荐平台：{platforms_text}。"
                )
            return (
                f"「{proj}」支持自部署，可写『Lighthouse 一键部署 {proj}』实操教程。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "竞品" in integration_text:
            comp_products = ", ".join(tcloud.get("tcloud_priority_matches", [])[:2]) or "竞品"
            return (
                f"「{title}」涉及 {comp_products} 动态，可从 Lighthouse 性价比优势切入做对比。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        # 根据项目的具体特性差异化描述
        techs = [t.strip() for t in (item.get("analysis", {}).get("techs", []) if isinstance(item.get("analysis", {}), dict) else [])]
        if not techs:
            # 从 item 的 tags 中提取
            techs = [t.strip() for t in str(item.get("tags", "")).split(",") if t.strip()]
        brief_lower = (item.get("summary", "") or item.get("description", "") or item.get("title", "")).lower()
        if "克隆" in integration_text or "私有" in integration_text or "github.com" in integration_text.lower():
            return (
                f"「{proj}」是开源项目，可写『克隆到 Lighthouse 自建私有实例』的教程，"
                f"强调数据自主可控。{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "gpu" in brief_lower or "training" in brief_lower or "simulation" in brief_lower:
            return (
                f"「{proj}」涉及 GPU 计算场景，适合结合 Lighthouse/CVM 做算力选型科普。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "trading" in brief_lower or "financial" in brief_lower or "金融" in brief_lower:
            return (
                f"「{proj}」是金融/交易类 AI 项目，可写『用 Lighthouse 低成本跑量化 Agent』的实战分享。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "coding" in brief_lower or "code" in brief_lower or "编码" in brief_lower:
            return (
                f"「{proj}」是 AI 编程工具，可写『Lighthouse + {proj} 打造私有编程助手』的教程。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "pdf" in brief_lower or "document" in brief_lower or "parser" in brief_lower:
            return (
                f"「{proj}」涉及文档处理，可写『Lighthouse 部署 {proj} 搭建私有文档解析服务』的教程。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if "plugin" in brief_lower or "extension" in brief_lower or "插件" in brief_lower:
            return (
                f"「{proj}」是插件/扩展工具，可作为开发者工具链话题带出 Lighthouse 的开发环境场景。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if is_discussion:
            return (
                f"「{title}」话题可从 Lighthouse 的开发者场景切入，"
                f"做产品植入或开发者经验分享。{priority_reason}，推荐平台：{platforms_text}。"
            )
        return (
            f"「{proj}」可部署到 Lighthouse 上运行，适合写『从零搭建 {proj} 私有实例』的入门教程。"
            f"{priority_reason}，推荐平台：{platforms_text}。"
        )

    # --- EdgeOne 相关 ---
    if primary_product == "EdgeOne":
        if "安全" in integration_text or "waf" in integration_text.lower():
            return (
                f"「{proj}」涉及 Web 安全场景，可写 EdgeOne 的 DDoS 防护 + WAF 实践指南。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        if is_discussion:
            return (
                f"「{title}」话题涉及网站加速/安全，可带出 EdgeOne 的 CDN + 安全方案。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        return (
            f"「{proj}」的网站/API 可用 EdgeOne 加速防护，适合写实践教程。"
            f"{priority_reason}，推荐平台：{platforms_text}。"
        )

    # --- 其他产品通用 ---
    product_map = {
        "Hunyuan3D": "3D 内容生成",
        "COS": "对象存储",
        "TDSQL-C": "云原生数据库",
        "ES": "搜索/检索",
        "CVM": "GPU 算力",
        "IM": "即时通信",
        "TRTC": "实时音视频",
    }
    scenario = product_map.get(primary_product, "")
    if scenario:
        if is_discussion:
            return (
                f"「{title}」话题涉及{scenario}场景，"
                f"可从 {product_display} 的能力切入做内容。"
                f"{priority_reason}，推荐平台：{platforms_text}。"
            )
        return (
            f"「{proj}」涉及{scenario}场景，"
            f"可结合 {product_display} 写教程或场景解读。"
            f"{priority_reason}，推荐平台：{platforms_text}。"
        )

    # 极端兜底
    if is_discussion:
        return (
            f"「{title}」可从 {product_display} 的使用场景切入参与讨论。"
            f"{priority_reason}，推荐平台：{platforms_text}。"
        )
    return (
        f"「{proj}」可结合 {product_display} 写应用实践内容。"
        f"{priority_reason}，推荐平台：{platforms_text}。"
    )


def build_content_strategy(
    item: Dict[str, Any],
    source_key: str,
    topic: str,
    tone: str,
    tags: Dict[str, List[str]],
    tcloud: Dict[str, Any],
    scores: Dict[str, float],
    detail_brief: str,
) -> Dict[str, Any]:
    topic_type = _derive_topic_type(
        source_key,
        topic,
        tone,
        tags.get("competitors", []),
        tags.get("competitor_products", []),
        tags.get("techs", []),
    )
    angles = _derive_editorial_angles(
        topic_type,
        topic,
        tone,
        tcloud.get("tcloud_products", []),
        tags.get("competitor_products", []),
        tags.get("techs", []),
        source_key,
    )
    timeliness_score = scores.get("timeliness_score", 0)
    composite_score = scores.get("composite_score", 0)

    priority = _derive_publish_priority(topic_type, timeliness_score, composite_score)
    priority_reason = _derive_priority_reason(topic_type, timeliness_score, composite_score)
    platforms = _derive_platform_reason(topic_type, source_key)

    angles_text = " + ".join(angles)
    platforms_text = " + ".join(platforms)

    # 构建可用角度的详细版本
    angle_detail = _build_angle_detail(
        item, topic_type, angles,
        detail_brief, tcloud,
        tags.get("competitor_products", []),
        tags.get("techs", []),
    )

    # 结构化结论：直接可执行
    editorial_brief = (
        f"话题类型：{topic_type}\n"
        f"可用角度：{angle_detail}\n"
        f"优先级：{priority_reason}\n"
        f"适合平台：{platforms_text}"
    )

    # 社媒结论：差异化的一句话运营指令，每条热点不同
    primary_product = tcloud.get("tcloud_primary_product") or ""
    product_display = _product_display_name(primary_product)
    social_recommendation = _build_social_recommendation(
        item, source_key, topic_type, primary_product, product_display,
        tcloud, angles_text, priority, priority_reason, platforms_text,
    )

    writing_pack = build_writing_pack(item, detail_brief, topic_type, angles_text, tcloud, source_key)

    return {
        "topic_type": topic_type,
        "editorial_angles": angles,
        "editorial_angles_text": angles_text,
        "publish_priority": priority,
        "recommended_platforms": platforms,
        "recommended_platforms_text": platforms_text,
        "editorial_brief": editorial_brief,
        "social_recommendation": social_recommendation,
        "writing_pack": writing_pack,
    }


# ============================================================
# 8. 主入口：分析单条 / 批量分析
# ============================================================


def analyze_item(item: Dict[str, Any], source_key: str = "") -> Dict[str, Any]:
    relevance = score_relevance(item)
    topic = classify_topic(item)
    tags = extract_tags(item)
    tone = annotate_tone(item)
    detail_brief = build_detail_brief(item, source_key)

    tcloud = evaluate_tcloud_integration(item, relevance, detail_brief, source_key)
    scores = compute_composite_score(
        item.get("heat", ""),
        item.get("time", ""),
        relevance,
        tcloud,
    )
    strategy = build_content_strategy(
        item,
        source_key,
        topic,
        tone,
        tags,
        tcloud,
        scores,
        detail_brief,
    )

    writing_pack = strategy["writing_pack"]

    return {
        "is_cloud": relevance["is_cloud"],
        "is_ai": relevance["is_ai"],
        "is_anxiety": relevance["is_anxiety"],
        "cloud_score": relevance["cloud_score"],
        "ai_score": relevance["ai_score"],
        "anxiety_score": relevance["anxiety_score"],
        "cloud_hits": relevance["cloud_hits"],
        "ai_hits": relevance["ai_hits"],
        "anxiety_hits": relevance["anxiety_hits"],
        "topic": topic,
        "topic_type": strategy["topic_type"],
        "products": tags["products"],
        "competitors": tags["competitors"],
        "competitor_products": tags["competitor_products"],
        "techs": tags["techs"],
        "tone": tone,
        "detail_brief": detail_brief,
        "heat_score": scores["heat_score"],
        "timeliness_score": scores["timeliness_score"],
        "composite_score": scores["composite_score"],
        "tcloud_relevance": tcloud["tcloud_relevance"],
        "tcloud_integration": tcloud["tcloud_integration"],
        "tcloud_products": tcloud["tcloud_products"],
        "tcloud_reasons": tcloud["tcloud_reasons"],
        "tcloud_evidence": tcloud["tcloud_evidence"],
        "tcloud_primary_product": tcloud["tcloud_primary_product"],
        "tcloud_relation_type": tcloud["tcloud_relation_type"],
        "tcloud_official_storyline": tcloud["tcloud_official_storyline"],
        "tcloud_official_matches": tcloud["tcloud_official_matches"],
        "tcloud_priority_matches": tcloud["tcloud_priority_matches"],
        "editorial_angles": strategy["editorial_angles"],
        "editorial_angles_text": strategy["editorial_angles_text"],
        "publish_priority": strategy["publish_priority"],
        "recommended_platforms": strategy["recommended_platforms"],
        "recommended_platforms_text": strategy["recommended_platforms_text"],
        "editorial_brief": strategy["editorial_brief"],
        "social_recommendation": strategy["social_recommendation"],
        "official_product_focus": writing_pack["product_display"],
        "official_angle_cn": writing_pack["official_angle_cn"],
        "visual_brief": writing_pack["visual_brief"],
        "writing_pack_summary": writing_pack["summary"],
        "platform_copies": writing_pack,
    }



def analyze_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for result in results:
        if result.get("error"):
            continue
        source_key = result.get("key", "")
        for item in result.get("items", []):
            item["analysis"] = analyze_item(item, source_key)
            # 中文简介 = 具体内容摘要的中文翻译 + 精简版
            detail_brief = item["analysis"].get("detail_brief", "")
            cn_summary = _generate_cn_summary(item, source_key, detail_brief)
            if cn_summary:
                item["summary_cn"] = cn_summary
    return results
