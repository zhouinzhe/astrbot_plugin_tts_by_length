# TTS 长短分流（astrbot_plugin_tts_by_length）

短句开口说，长文发文字：**短文本调用 TTS，长文本直接发文字。** 朗读前自动过滤链接、代码块、Emoji 等不适合读出来的内容。

仓库：https://github.com/zhouinzhe/astrbot_plugin_tts_by_length

## 使用
1. 插件管理 → 上传 ZIP 安装并启用。
2. AstrBot 自带 TTS：保持「启用文本转语音」开启并选好 TTS 提供商，但把「TTS 触发概率」设为 **0**（由本插件接管，否则长文本会被内置 TTS 再转一遍）。
3. 在插件配置里设置 `max_chars`（默认 80）。
4. 关闭流式输出，否则流式回复会跳过发送前钩子，TTS 不会触发。

## 朗读时过滤什么
- **链接**：`https://…`、`www.…` 不朗读；Markdown 链接 `[文字](url)` 只读文字；`![图](url)` 整体丢弃。
- **代码块**：含 ``` 代码块的回复默认整条发文字。
- **Emoji / 符号**：默认去掉，避免 TTS 读错。
- **@**：消息链里带 @ 组件，或正文里有 `@QQ号`、`[At:123456]`、`[CQ:at,…]` 时，整条发文字、不调用 TTS（`skip_if_at`）；关掉这个开关后，这些 @ 文本也会被过滤、不会被读出来（`strip_at_text`）。
- **自定义**：`custom_filter_patterns` 里填正则，例如 `（.*?）` 去掉括号动作。

长度按过滤后的文本计算，所以带一长串链接的短回复仍然会转语音。

## 链接怎么处理（url_action）
| 值 | 效果 |
|---|---|
| append_links（默认） | 语音不读链接，链接以文字跟在语音后面 |
| strip | 语音不读，链接直接丢掉 |
| send_text | 只要有链接，整条发文字、不转语音 |

## 其他配置
| 项 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| max_chars | 80 | 超过就发文字；0 = 不限 |
| min_chars | 1 | 短于该长度不转语音 |
| trigger_probability | 1.0 | 短文本转语音的概率 |
| tts_timeout | 30 | TTS 超时秒数，超时自动发文字；0 = 不限时 |
| keep_text | false | 转语音时同时保留原文（此时不再单独追加链接） |
| only_llm_result | true | 只处理大模型回复 |
| skip_if_at | true | 含 @ 时整条发文字、不转语音 |
| strip_at_text | true | 朗读时去掉 @QQ号 / [At:…] 等文本 |
| skip_platforms | [] | 跳过的平台名 |

过滤后没有可读内容（只有链接、只有 Emoji 等）时不转语音，保持原文。

## 群聊注意事项
- 只识别 `@数字ID`、`[At:…]`、`[CQ:at,…]` 和真正的 @ 组件；`@昵称` 这种没有固定边界的写法不会被识别，需要的话可用 `custom_filter_patterns` 自己加正则。
- 智能体（Agent）用 `send_message_to_user` 等工具直接发出的消息不经过发送前钩子，本插件管不到，它们始终是文字。
- 如果 AstrBot 全局开启了「回复时 @ 发送者 / 引用回复」，框架会在插件之后才加上 @ 和引用，插件看不到，语音是否能带上它们取决于平台。
