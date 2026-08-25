# 守望 SHOUWANG

**微博理性发言主动干预插件**：在你按下微博发送按钮之前，检测正文中的攻击性 / 负面内容，通过提示条或弹窗温和劝导，给冲动一个缓冲。

## 架构

浏览器扩展（Manifest V3）+ 本地后端（FastAPI），后端两层判定：

1. **MacBert 本地快检**（主力）：ONNX INT8 量化模型，CPU 推理、零网络依赖，单条约 34ms
2. **DeepSeek 大模型兜底**：对模糊区间做深度核查，并承担劝导对话

## 开源资源

| 资源 | 说明 | 地址 |
| --- | --- | --- |
| 模型「望潮 TideWatcher」 | MacBert 攻击检测模型（ONNX INT8，98 MB） | https://huggingface.co/RainbowLIght/tidewatcher-macbert-c2 |
| 数据集「暗流」 | 训练与评测数据集 | 待发布 |

## 环境配置

- 需要 **Python 3.9+**
- 安装后端依赖：

```bash
pip install -r backend/requirements.txt
```

- 国内网络无法直连 HuggingFace 时，先设置镜像端点再执行模型相关命令：

```bash
# PowerShell
$env:HF_ENDPOINT="https://hf-mirror.com"
# Linux / macOS
export HF_ENDPOINT=https://hf-mirror.com
```

## 获取模型

一键拉取并自动放置到插件期望的目录结构：

```bash
python backend/setup_models.py
```

也可按模型主页说明手动下载（https://huggingface.co/RainbowLIght/tidewatcher-macbert-c2）。

## 配置 API Key

后端使用 DeepSeek 大模型做深度核查与劝导对话。从配置模板复制一份并填入你的 key：

```bash
cd backend
copy .env.example .env    # Windows
# cp .env.example .env    # Linux / macOS
```

编辑 `.env`，把 `DEEPSEEK_API_KEY` 替换成你的 DeepSeek API key（在 https://platform.deepseek.com/ 获取）。

> 没有 key 也能运行：快检与规则层正常工作，仅深度核查与劝导对话降级不可用。

## 插件安装与运行

**1. 启动后端**

```bash
python backend/app.py
```

服务运行于 http://127.0.0.1:8000 ，可用浏览器访问 `http://127.0.0.1:8000/healthz` 验证。

**2. 加载浏览器扩展**

- Chrome 打开 `chrome://extensions`（Edge 打开 `edge://extensions`）
- 开启右上角「开发者模式」
- 点击「加载已解压的扩展程序」，选择本仓库的 `extensions/` 目录
- 出现「微博理性发言主动干预插件」即加载成功

**3. 使用**

打开微博（weibo.com / weibo.cn），在评论区输入文字。发送前会自动检测：命中攻击性内容弹**警告弹窗**，纯负面情绪显示**提示条**，可对话式重新表达。

## 目录结构

```
├── backend/                 # FastAPI 后端
│   ├── app.py               # 服务入口（快检 + 深度核查 + 劝导对话）
│   ├── macbert_detector.py  # MacBert ONNX 快检模块（含降级逻辑）
│   ├── setup_models.py      # 一键拉取模型
│   ├── requirements.txt     # 依赖清单
│   └── .env.example         # 环境变量模板（复制为 .env 使用）
├── extensions/              # 浏览器扩展（MV3）
│   ├── manifest.json
│   └── content.js
└── tidewatcher-model/       # 模型开源说明（model card 与下载脚本）
```

## 引用与致谢

- 模型架构：[hfl/chinese-macbert-base](https://huggingface.co/hfl/chinese-macbert-base)（HuggingFace）
- 公开数据集：[COLD 中文冒犯性语言数据集](https://github.com/thu-coai/COLDataset)（Apache-2.0）、[weibo_senti_100k](https://github.com/SophonPlus/ChineseNlpCorpus)（仅研究参考）
- 核心依赖：[transformers](https://github.com/huggingface/transformers)、[onnxruntime](https://github.com/microsoft/onnxruntime)、[FastAPI](https://github.com/fastapi/fastapi)、[SnowNLP](https://github.com/isnowfy/snownlp)
- 大模型服务：[DeepSeek](https://platform.deepseek.com/)

## 许可证

[Apache License 2.0](LICENSE)
