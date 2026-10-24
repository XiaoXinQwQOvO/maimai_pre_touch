# maimai_pre_touch — 手机当 maimai 触屏

把 PC 上运行的 maimai DX（SDEY / micetools 环境）画面实时推到手机，手机全屏显示并采集触摸，
触摸坐标按「环形分区」映射后回传给 PC，再由 PC 注入给游戏。

```
┌──────────── PC (Windows) ────────────┐        ┌────────── 手机 (Android) ──────────┐
│ maimai.exe ──DXGI 抓屏──► NVENC H.264│        │ WebView (APK) / 浏览器             │
│      ▲                     │         │        │   ├── <video> 播放 WebRTC 视频      │
│      │ W3C 指针触摸         ▼         │  Wi-Fi │   └── 触摸层：坐标 → 分区 → 归一化 │
│ 触摸注入后端 ◄──── aiohttp + aiortc ──┼───────►│                                    │
│ (触控/端口)        WebSocket 信令/触摸 │        │                                    │
└──────────────────────────────────────┘        └────────────────────────────────────┘
```

## 组件

| 目录 | 内容 |
| --- | --- |
| `pc/` | PC 服务端：抓屏、H.264(NVENC) 编码、WebRTC 推流、WebSocket 信令与触摸接收、分区映射、触摸注入 |
| `web/` | 手机端网页客户端（由 PC 提供，APK 只是外壳，改前端不用重装 APK） |
| `android/` | 原生 APK 外壳（全屏沉浸 + WebView + 服务器地址设置），**免 Gradle** 用 aapt2/javac/d8 构建 |
| `tools/` | 工具链安装、编码器自测、无手机端到端自测、逆向探针脚本 |
| `docs/` | 架构说明、逆向笔记 |

## 快速开始

### 1. PC 端

```powershell
pip install -r pc\requirements.txt
run_server.bat                 # 或 python pc\server.py
```

启动后会打印形如：

```
 打开手机浏览器 / APK 指向:  http://192.168.31.27:8770/
```

（端口默认 `8770`，`pc/config.json` 可改。想让 PC 直接自测可以先跑
`python pc\server.py --touch log`：只记录触摸，不注入。）

### 2. 手机端

**方式 A：装 APK（推荐）**

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_android_toolchain.ps1   # 首次，下载 JDK17 + Android SDK
powershell -ExecutionPolicy Bypass -File android\build.ps1                   # 产出 android\maimai_touchpanel.apk
powershell -ExecutionPolicy Bypass -File android\build.ps1 -Install          # 顺便 adb install -r
```

首次打开会要求填 PC 地址（例如 `192.168.31.27:8770`）。
之后：**长按返回键**可改地址，**连按两次返回键**退出。

**方式 B：浏览器**

手机浏览器直接打开 `http://<PC-IP>:8770/`。Chrome / 系统 WebView 皆可（需要支持 WebRTC + H.264 解码）。

### 3. 校准

点画面右下角 ⚙：

* 中心 X/Y、最内环、最外环、扇区角度偏移：调整分区网格，实时叠加显示；
* 「显示分区编号」：显示 A1…E8；
* 「吸附到分区中心」：触摸时把坐标吸附到所在分区中心（判定更宽松）；
* 「应用到服务器」：把分区参数同步给 PC（PC 侧只用于日志/测试，注入以手机发来的坐标为准）。

参数存在手机 localStorage，可随时「恢复默认」。

## 触摸是怎么进游戏的

用户的硬性要求是 **不走鼠标、走端口**。当前实现与后续路线：

1. **现在（默认）**：`SyntheticTouchBackend` 用
   `CreateSyntheticPointerDevice` + `InjectSyntheticPointerInput` 注入 **真实的多点触控(W3C pointer / PT_TOUCH)**
   事件——对游戏而言与物理触摸屏无差别，不是鼠标、不移动光标、不点击鼠标键。
2. **目标（逆向中）**：micetools 把 `com3=maitouch` 声明为触摸串口，游戏从该端口读触摸状态。
   找到该状态在进程内的位置后，`MaitouchStateBackend` 可直接写状态，等于从「端口数据」这一层注入。
   进展见 [`docs/re_maitouch.md`](docs/re_maitouch.md)。

后端在 `pc/config.json` 的 `touch.backend` 切换：`synth`（默认）/ `log`（干跑）/ `maitouch`（待接入）。

## 画面链路

* 抓屏：`dxcam`（DXGI Desktop Duplication），只抓游戏窗口客户区（本项目实测 960×960 @60fps，16.6 ms/帧）；
* 编码：**NVENC** `h264_nvenc`（`preset=p1, tune=ull, rc=cbr`），实测 **1.41 ms/帧**（≈700fps 余量），
  通过替换 `aiortc.rtcrtpsender.get_encoder` 接入；无 NVENC 时自动回落 `libx264`；
* 传输：WebRTC（SRTP/UDP），H.264 constrained baseline，`packetization-mode=1`；
* 不传声音（声音留在 PC 音箱）。

## 协议（WebSocket `/ws`）

| 方向 | 消息 |
| --- | --- |
| PC→手机 | `{"t":"welcome", zone_map, video, window, touch_backend}` |
| 手机→PC | `{"t":"offer","sdp":...}` → PC 回 `{"t":"answer","sdp":...}` |
| 手机→PC | `{"t":"touch","id":1..10,"a":"down|move|up","x":0..1,"y":0..1}` |
| 手机→PC | `{"t":"zone_map", zone_map:{...}}`、`{"t":"ping","ts":毫秒}` |
| PC→手机 | `{"t":"pong","ts",...}`、`{"t":"stats",...}` |

`x,y` 是**游戏画面内的归一化坐标**（0,0 = 画面左上角，1,1 = 右下角），与手机分辨率、留黑边无关。

## 自测（不需要手机）

```powershell
python pc\server.py --touch log --region 0,0,960,960     # 一个终端
python tools\test_client.py --url 127.0.0.1:8770         # 另一个终端：模拟手机
```

`test_client.py` 会完整走一遍 WebRTC 协商、收视频帧、发触摸事件，最后打印 `PASS/FAIL`
（实测：960×960 视频正常，WS 往返 1–7 ms）。

其他工具：

```powershell
python tools\test_encoder.py     # NVENC 编码器 + 解码回环验证
python tools\bench_video.py      # 抓屏帧率 / 编码耗时基准
```

## 常见问题

* **连不上**：确认手机与 PC 同一网段；Windows 防火墙放行 `python.exe`（或 `8770` TCP）。
* **有画面但触摸没反应**：先用 `--touch log` 看服务端是否收到触摸；收到但游戏无反应则看
  `http://<PC-IP>:8770/api/status` 里的 `touch.errors`。
* **卡顿/掉帧**：`pc/config.json` 里把 `video.bitrate_kbps` 降到 4000–6000，或 `video.fps` 降到 30。
* **画面不是游戏**：游戏窗口标题 `Micetools`、类名 `teaGfx`；窗口被其它窗口遮挡不影响抓屏。

## 免责 / 边界

本项目只做「显示 + 触摸转发」，不修改游戏文件、不做鼠标模拟、不联网校验绕过。
涉及的 `mice.dll` / `maimai*.exe` 一律只读、只做备份，不写入。
