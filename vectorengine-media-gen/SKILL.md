---
name: vectorengine-media-gen
description: "通过 RelayRouter 生成图片，保留原 vectorengine-media-gen 命令入口。仅接入 GPT Image 2.5 Flare/Sunburst、Gemini 3.1 Flash Image 和 Seedream 5.0 Pro；默认 Flare。支持受控回退、真实图片校验和文件交付，不提供视频或音频生成。"
metadata: {"openclaw":{"primaryEnv":"VECENGINE_API_TOKEN","requires":{"bins":["python3"],"env":["VECENGINE_API_TOKEN"]}}}
---

# VectorEngine Media Generator — RelayRouter 后端

Skill 名称保持 **`vectorengine-media-gen`**，后端统一使用 `https://api.relayrouter.ai`。
不要再调用旧 VectorEngine、fal 或 Replicate 路由，不保留旧型号别名。

## 密钥与安装

与 TikHub Skill 相同：**OpenClaw 提供环境变量，脚本只从环境变量读取 Key**。

变量名为 `VECENGINE_API_TOKEN`，可在 OpenClaw 主机的
`~/.openclaw/openclaw.json` 中配置 `env.vars.VECENGINE_API_TOKEN`。
`primaryEnv` / `requires.env` 声明让 OpenClaw 正确识别凭据需求。
仓库不包含 API Key 或主机配置；每次在新环境安装时须配置自己的 RelayRouter Key。

- Key 不写进本 Skill、模型注册表、prompt、命令参数、日志或 Git。
- 不读取 `openclaw.json` 或凭据文件来绕过环境注入。
- 不使用其他服务的 Key 兜底。缺少变量时明确失败。
- `--token` / `-t` 已移除；调用方使用 OpenClaw 正常环境注入。
- 新环境可使用 `skills.entries.vectorengine-media-gen.apiKey` 配合 `primaryEnv` 注入；
  但已存在的进程环境变量优先，不要让新专属 Key 被旧全局环境值覆盖。
- 修改全局环境配置可能需要 Gateway 重启；不要自行重启共享服务，先告知影响并获得确认。
- 直接 SSH 登录的 shell 不一定继承 Gateway 环境，不能据此判断 Key 未配置。

安装时将整个 `vectorengine-media-gen/` 目录复制到
`~/.openclaw/skills/vectorengine-media-gen/`，不要只复制 `SKILL.md`。
需要 Python 3.11+ 和 Pillow，在该目录安装依赖：

```bash
cd ~/.openclaw/skills/vectorengine-media-gen
python3 -m pip install -r requirements.txt
```

已有兼容 Pillow 时不必重复安装。主机运行时依赖与独立沙箱依赖须分别确认。

## 四个模型：默认及回退顺序

唯一模型清单位于 `config/models.json`，严格按以下顺序：

| 顺序 | 精确模型 ID | 接口 |
|---|---|---|
| 1，默认 | `gpt-image-2.5-flare` | POST `/v1/images/generations` |
| 2 | `gpt-image-2.5-sunburst` | POST `/v1/images/generations` |
| 3 | `gemini-3.1-flash-image` | POST `/v1beta/models/gemini-3.1-flash-image:generateContent` |
| 4 | `doubao-seedream-5-0-pro-260628` | POST `/api/v3/images/generations` |

Gemini 使用此处精确名称，不改成 `-preview`；该 ID 已在授权模型目录中确认存在。
模型目录可见不等于生图一定成功，具体可用性以实际响应为准。

未传 `--model` 时从 Flare 开始；只有明确可回退的 429/503 错误才进入下一项，
每个模型最多提交一次。指定 `--model` 时只调用该模型，不自动换型号。
调用方不得再另建一条重试或 Azure 回退链。

## 调用

```bash
cd ~/.openclaw/skills/vectorengine-media-gen

# 不需要 Key 或网络即可列出模型
python3 scripts/generate.py --media image --list-models

# 默认模型与受控回退
python3 scripts/generate.py --media image \
  --prompt "白色背景，橙色圆形和蓝色方形，简洁平面插画，无文字" \
  --size "1:1" --resolution 1K --num 1

# 精确指定模型：不启用回退
python3 scripts/generate.py --media image \
  --model gpt-image-2.5-sunburst \
  --prompt "产品静物，影棚光线，无文字" \
  --size "3:4" --resolution 1K --num 1
```

| 参数 | 含义 |
|---|---|
| `--media image` | 当前仅实现图片；video/audio 明确报不支持 |
| `--prompt` / `-p` | 必填文本；GPT 两模型最多 1000 字符 |
| `--model` / `-m` | 可选，必须是上表精确 ID；不接受旧别名 |
| `--size` / `-s` | 比例或合法像素尺寸，默认 `3:4` |
| `--resolution` / `-r` | `1K` / `2K` / `4K`，默认 `1K`；受模型限制 |
| `--num` / `-n` | 默认 1；GPT 支持本 CLI 的 1–4，Gemini/Seedream 当前限 1 |
| `--output-dir` / `-o` | 可选，默认 `~/.openclaw/media/vecengine/pic/` |
| `--timeout` | 每次请求默认 300 秒，允许 10–600 秒；超时不会自动重提 |

### 尺寸和请求格式

- GPT 2.5 使用网关字段 **`format`**，不是旧的 `output_format`；显式发送 `n`。
  尺寸两边须为 16 的倍数，最长边不超过 3840，宽高比不超过 3:1，
  总像素 655360–8294400。高分辨率可能增加成本，超出常规档位会在摘要中标记。
- Gemini 使用 `contents` 和 `generationConfig.imageConfig`，分别传
  `aspectRatio`、`imageSize`。像素输入只用于确定比例，不能承诺精确像素输出。
- Seedream 使用 `/api/v3` 原生接口，只支持文档列出的 1K/2K 像素尺寸；
  比例参数映射后发送实际像素值。1K 的 16:9 档为文档中的近似 `1424x800`。
- 显式像素尺寸优先于分辨率档位，摘要会注明 `resolution_ignored`；
  不默默把不支持的参数丢弃或改成另一种输出要求。
- 本版本只做文生图，不把 Ideogram Describe、视频任务或图像编辑接口混作生图。

## 结果与交付

完整验证 PNG/JPEG/WebP 后才保存文件：检查格式、解码、尺寸与像素上限，
不将 HTML 错误页或损坏字节保存为“成功图片”。不覆盖已有文件。

保留旧调用方识别的输出：

```text
SAVED: <实际文件路径> (<字节数> bytes)
{"media":"image","files":["<实际文件路径>"],"model":"<实际使用的模型>","prompt":"<原始prompt>", ...}
```

JSON 同时含实际图片尺寸、请求设置与尝试记录。模型生成之后的图片下载
不携带 API Key；重定向、DNS/IP 和文件大小均受限制。
得到 `SAVED:` 后，还应检查中文是否正确、布局是否清晰，再用 `MEDIA:<path>` 交付。
文件能解码不代表文案、内容或视觉质量已通过人工验收。

## 错误和异步边界

- 失败输出安全的错误 JSON，并以非零状态退出；不输出完整上游错误正文。
- 401/402/403：鉴权、余额或权限问题，停止，不换 Key、不重复提交。
- 内容审核拒绝：停止，不通过回退规避。
- 超时、连接中断或其他状态不明：可能已被受理，标为 `unknown_outcome`，不自动重提。
- 图片下载失败：不重新生图，避免重复计费。
- Seedream 若只返回 `task_id`：标为 `pending`、保留安全任务标识并停止。
  当前文档没有明确配对的图片轮询接口；不猜测查询路径，也不把提交成功宣称为出图完成。
- 默认每模型一次，调用方不可叠加 `timeout ...` 后自动重试的脚本。

## 开发与来源

2026-09-17 使用新 Key 对四个精确模型各执行一次 1:1 简单图形生成：
全部命令成功、各返回一张 1024×1024 可解码图片；Gemini 返回 JPEG，其余返回 PNG。
本次 Seedream 直接返回完成图片。此结论不覆盖全部尺寸、多图请求、编辑或异步任务。

离线测试不调用真实 API：

```bash
python3 -B -W error -m unittest discover -s tests -q
```

真实生图另行获得费用与次数授权，不把离线通过或模型列表可见标成实测成功。

官方资料：
- https://doc.relayrouter.ai/en/reference/ideogram?op=post-ideogram-describe
- 同站 OpenAI Images 文档：`/v1/images/generations`
- 同站 Gemini 原生文档：`/v1beta/models/{modeName}:generateContent`
- 同站 Seedream 5.0 Pro 文档：`/api/v3/images/generations`
