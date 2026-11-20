# OpenCode Go -> DeepSeek Harness 本地反代

把 OpenCode Go 封装成本地 OpenAI 兼容端点，供 DSH 或任何兼容客户端使用。单文件、纯标准库、无配置文件，key 只走环境变量。

```text
client --HTTP--> 127.0.0.1:8787 --HTTPS--> opencode.ai/inference/go
```

它做三件事：key 不出本进程；把 `/v1/*` 映射到 Go 网关；补 `x-opencode-session`（缺了会 `400 MissingSessionID`），而 DSH 不带（[官方说明](https://opencode.ai/docs/console/go#known-problematic-clients)）。

## 快速开始

需要 Python 3.9+、Go 订阅和 personal API key（`oc_sk_...`）。用 DeepSeek 模型前，先把 Console -> Settings -> Privacy -> Regions 设为 **Global**。

```bash
export OPENCODE_GO_API_KEY="oc_sk_..."   # 可写进 ~/.bashrc 持久化
python3 proxy.py start -b                # 后台，日志在 ./logs/
python3 proxy.py status                  # 退出码 0 = 在跑
python3 proxy.py stop
```

验证：

```bash
curl -s http://127.0.0.1:8787/v1/models | head -c 200

curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"say ok"}],"max_tokens":32}'
```

调用日志：`logs/proxy-<port>.out.log`，每次一行（模型、状态、耗时、会话 ID）。

> Windows：`export X=...` 换成 `$env:X="..."`（持久化用 `setx`），`~/` 即 `%USERPROFILE%\`。

## 接入 DSH

写进 `~/.dsh/settings.yaml`（桌面版也可用 `~/.dsh/profiles/desktop/cordis.patch.yml`，只维护一处）：

```yaml
llm-pi-ai:
  providers:
    opencode-go:
      displayName: OpenCode Go (local proxy)
      api: openai-completions
      baseURL: http://127.0.0.1:8787/v1
      # 占位：真实 key 在反代进程里，这里任何非空值都行
      apiKeyEnv: OPENCODE_GO_LOCAL_KEY
      # DeepSeek 线上格式：思考内容走 reasoning_content，输出上限字段是 max_tokens
      compat:
        thinkingFormat: deepseek
        requiresReasoningContentOnAssistantMessages: true
        maxTokensField: max_tokens
        supportsStore: false
      models:
        - id: deepseek-v4.1-flash
          name: DeepSeek V4.1 Flash
          contextWindow: 1000000
          maxTokens: 384000
          input: [text, image]
          reasoningEfforts:
            low: low
            high: high
            max: max
        - id: deepseek-v4-pro
          name: DeepSeek V4 Pro
          contextWindow: 1000000
          maxTokens: 384000
          input: [text]
          reasoningEfforts:
            high: high
            max: max
        - id: glm-5.3
          name: GLM-5.3
          contextWindow: 1000000
          maxTokens: 131072
          input: [text]
          # GLM 的思考参数用另一套格式，覆盖路由级的 deepseek
          compat:
            thinkingFormat: zai
            supportsReasoningEffort: true
          reasoningEfforts:
            low: low
            high: high
            max: max

agent-default-model:
  provider: opencode-go
  model: deepseek-v4.1-flash
  reasoningEffort: max
```

`apiKeyEnv` 只是占位，真实 key 在反代里。`~/.dsh/.env` 加一行：

```dotenv
OPENCODE_GO_LOCAL_KEY=local
```

重启 DSH，在 `/models` 选 `DeepSeek V4.1 Flash`。

## 其他兼容客户端

```text
Base URL : http://127.0.0.1:8787/v1
API Key  : 任意非空字符串（反代会换成真实 key）
Model    : deepseek-v4.1-flash / deepseek-v4-pro / glm-5.3
```

## 说明

- 只监听 `127.0.0.1`，无鉴权，别暴露到公网。
- 必须常驻：进程一停客户端就连不上，重启机器后要重新 `start -b`（自启用 systemd、launchd 或任务计划程序）。
- 官方 `npx @opencode/console setup deepseek-harness` 不补会话头；可先用它接上 DSH，再把 `baseURL` 指向本地。
- 非官方工具，与 OpenCode / DeepSeek 无关；MIT，见 [LICENSE](LICENSE)。
