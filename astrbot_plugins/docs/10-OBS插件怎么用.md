# 10 · OBS 插件怎么用（有就用，没有就当它不存在）

## 它是什么

让「幕」能连 OBS：查状态、列场景、切场景、录屏开关、直播开关。

**没有 OBS 的人不需要做任何事**——插件探测不到 OBS 时会说一句人话就退下，
不报错、不刷日志、不反复重连。

---

## 二、装之前先开 OBS 的 WebSocket

```
OBS → 菜单「工具」→「WebSocket 服务器设置」
  ☑ 启用 WebSocket 服务器
  端口：4455（默认）
  ☑ 启用身份验证 → 设一个密码（强烈建议）
```

OBS 28 以上自带 obs-websocket v5，不需要另外装插件。

## 三、配置（可选）

把 `config.example.json` 复制成 `config.json`，放在插件目录旁：

```json
{
  "obs_host": "127.0.0.1",
  "obs_port": 4455,
  "obs_password": "你在 OBS 里设的密码",
  "allow_stream_start": false,
  "req_timeout": 8.0,
  "fail_window": 120,
  "fail_limit": 3
}
```

改完重载插件或重启 AstrBot。

## 四、五个工具

| 工具 | 干什么 | 说明 |
|---|---|---|
| `obs_state` | 版本、当前场景、是否在录/播 | 最常用，先问它 |
| `obs_scenes` | 列出所有场景 | 切场景前先查，别让模型自己编名字 |
| `obs_switch_scene(name)` | 切到指定场景 | 名字必须和 OBS 里完全一致，不存在会告诉你现有的是哪些 |
| `obs_record(action)` | start / stop / toggle / status | 录制 |
| `obs_stream(action)` | start / stop / toggle / status | 直播；**开播默认被禁** |

## 五、安全闸（代码保证，不靠模型自觉）

```
① 动作白名单     只认 start / stop / toggle / status，乱传动作直接拒绝
② 开播默认禁止   allow_stream_start=false；AI 不能自己开播，要开得你改配置
③ 熔断           同一动作 120 秒内失败 3 次 → 停手，并告诉你该检查什么
④ 先探测再连接   TCP 探测不通就直接回可读原因，不做盲目重连
⑤ 出口全是中文   不把异常抛给上层，不会因为 OBS 没开就把对话搞崩
```

## 六、验收怎么做的（没有 OBS 也能验）

本机没装 OBS，所以写了一台**假的 obs-websocket v5 服务端**跑真协议：

```powershell
python default-workspace\agent-neurofusion\tests\test_obs.py
```

它验 15 项：

```
握手认证（真算 challenge-response，密码错会走到 op=9）
取版本 / 当前场景 / 录制状态
列场景 / 切场景 / 切到不存在的场景
开始录制 / 查录制状态 / 停止录制
开播被拦、乱传动作被拒、熔断生效
端口不通时的可读提示
```

结果：**通过 15 / 失败 0**。

## 七、常见问题

**Q：装了插件但从来没反应？**
说明探测不到 OBS（没开或不在这台机器）。`obs_state` 会明确告诉你连不上和可能原因。

**Q：密码填错会怎样？**
握手会收到 `op=9`，插件回「认证失败：密码不对」。

**Q：能开播吗？**
默认不能。要能就改 `allow_stream_start: true`——这是**你自己动手**的决定，不是 AI 的。
