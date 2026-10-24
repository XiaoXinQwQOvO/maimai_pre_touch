# maimai_pre_touch — 手机当 maimai 触屏

把 PC 上运行的 maimai DX（SDEY / micetools 环境）画面实时推到手机，手机全屏显示并采集触摸，
触摸按「17 传感器面板」解算后**直接写进游戏的触摸串口数据**，由游戏自己当作触摸输入处理。

```
┌──────────── PC (Windows) ──────────────┐        ┌────────── 手机 (Android) ──────────┐
│ maimai.exe ──DXGI 抓屏──► NVENC H.264  │        │ WebView (APK) / 浏览器             │
│      ▲                       │         │        │   ├── <video> 播放 WebRTC 视频      │
│      │ maitouch 状态包        ▼         │  Wi-Fi │   └── 触摸层：坐标 → 17 传感器      │
│ 端口写入后端 ◄────── aiohttp + aiortc ──┼───────►│                                    │
│ (WriteProcessMemory)   WebSocket        │        │                                    │
└────────────────────────────────────────┘        └────────────────────────────────────┘
```

## 组件

| 目录 | 内容 |
| --- | --- |
| `pc/` | PC 服务端：抓屏、H.264(NVENC) 编码、WebRTC 推流、WebSocket 信令与触摸接收、传感器解算、端口写入 |
| `web/` | 手机端网页客户端（由 PC 提供，APK 只是外壳，改前端不用重装 APK） |
| `android/` | 原生 APK 外壳（全屏沉浸 + WebView + 服务器地址设置），**免 Gradle** 用 aapt2/javac/d8 构建 |
| `tools/` | 工具链安装、各类自测脚本、逆向探针 |
| `docs/` | 架构说明、maitouch 端口逆向报告 |

## 快速开始

### 1. 先启动游戏

用游戏目录里的 **`1start.bat`** 启动（它会改 `mice\config.ini` 的 `allnet` 为局域网 IP，并起
`mice\portproxy_naominet.py` 做 80 端口的 naominet 桥，然后拉起 `mice.exe`）。
**直接双击 `mice.exe` 会卡在黑屏** —— 没有那个桥，`ALLNetProc` 连不上 `192.168.31.27:80`。

起来后停在游戏的**待机/等待画面**（黑屏结束后的那个画面，左边有个大圆）就行，
服务端需要在这个画面里识别触摸面板圆。本项目实测窗口客户区 960×960 @ 屏幕 (800,240)。

> 抓屏走的是桌面合成结果，所以**别让别的窗口盖住游戏**（`mice.exe` 的控制台也算）。

### 2. PC 端服务

```powershell
pip install -r pc\requirements.txt
run_server.bat                 # 或 python pc/server.py
```

启动后打印：

```
 打开手机浏览器 / APK 指向:  http://192.168.31.27:8770/
```

### 3. 手机端

**方式 A：装 APK（推荐）**

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_android_toolchain.ps1   # 首次，下载 JDK17 + Android SDK
powershell -ExecutionPolicy Bypass -File android\build.ps1                   # 产出 android\maimai_touchpanel.apk
powershell -ExecutionPolicy Bypass -File android\build.ps1 -Install          # 顺便 adb install -r
```

首次打开填 PC 地址（例如 `192.168.31.27:8770`）；之后**长按返回键**改地址、**连按两次返回**退出。

**方式 B：浏览器**

手机浏览器打开 `http://<PC-IP>:8770/`（需要支持 WebRTC + H.264 硬解）。

### 4. 触摸面板

面板几何**不用手工校准**：游戏待机画面左下角那个圆就是触摸面板边界，服务端自动检测并锁定它
（圆心 = 面板中心，半径 = 面板半径），手机端画出来的环就压在那个圆上：

```
        A4  A3  A2
     A5   ┌───┐   A1
   A6  B5 │ C │ B1  A8       C  = 中心圆   r < 0.2241
     B6   └───┘   B8         B1-8 = 内环   0.2241 ≤ r < 0.5518
        B7  B4? B3           A1-8 = 外环   0.5518 ≤ r < 1.0
                              r ≥ 1.0 = 圆外，忽略
```

编号从画面**右侧（3 点方向）**开始、顺时针排列。设置面板里可开关网格/编号、
「吸附到传感器中心」（把触摸点吸到传感器中心，判定更宽松，默认开）。

**拉缩矫正**：检测出的横向/纵向半径是分开取的（`rx`、`ry`），画面被拉伸或压扁时圆会变成椭圆，
手机端的环也跟着画成同一个椭圆、触摸按同一个椭圆换算，所以永远和游戏里那个圆重合。

**微调**：手机设置页有四个滑杆（圆心 X/Y、半径 X/Y），拖动时立刻按新圆换算并重画环，
对齐游戏里那个圆以后点「保存到 PC」——值会写进 `pc/config.json` 的 `panel.manual`，
以后启动直接用它（想回到自动检测就勾回「跟随游戏自动检测」）。

面板状态可以在手机设置页或 `http://<PC-IP>:8770/api/panel` 看到：

| `state` | 含义 |
| --- | --- |
| `dark` | 画面还是黑屏（游戏在启动），等亮起来再检测 |
| `searching` / `lost` | 画面亮了但还没锁定（或曾经锁定后丢失），正在找圆 |
| `locked` | 已锁定，触摸按这个圆换算 |
| `manual` | 用 `pc/config.json` 里 `panel.manual` 手工指定 |
| `full` | 没检测到圆，回落到「整幅画面就是面板」（旧行为） |

锁定值会一直保留（面板本身不会动），画面切到选曲/游戏中也不会乱跳。
想手工固定（也可以在手机设置页拖滑杆后「保存到 PC」）：

```json
"panel": { "auto": false, "manual": { "cx": 0.2515, "cy": 0.7165, "rx": 0.2493, "ry": 0.2806 } }
```

看一眼检测结果对不对（会生成两张图，`_raw` 是原图）：

```powershell
python tools\panel_preview.py            # 抓当前画面 + 检测 + 画环 → tools\probe_out\panel_overlay.png
```

## 触摸是怎么进游戏的（走端口）

用户的硬性要求是**不走鼠标、走端口**，实现方式：

micetools 把 `com3=maitouch` 模拟成 maimai 的触摸串口，设备线程每 ~2 ms 往游戏推一个 14 字节状态包：

```
'(' + 12 个传感器字节 + ')'        每个字节 = 0x40 | 位掩码（0x40 = 未触摸）
只用 byte1–4 → 16 个环区 + 中心 = 17 个传感器
```

`mice.dll!build_status`（RVA `0x43FD`）是唯一会重写这个包的函数，它从鼠标/`WM_POINTER` 联系人表
推导触摸位。所以 PC 端做两件事：

1. 把 `build_status` 首字节 `0x55`(push ebp) 改成 `0xC3`(ret)，让鼠标不再驱动触摸状态；
2. `WriteProcessMemory` 把手机解算出来的 14 字节状态包写进 `mice.dll+0x18B040`。

游戏从 COM3 读到什么，就完全由我们决定 —— 这就是「走端口」。没有鼠标模拟、不注入代码洞、
用到的只有一个 1 字节的前导补丁。

安全性设计：

* **懒冻结**：只有手机真的按下时才冻结 `build_status`；
* **空闲自动恢复**：最后一次触摸后 5 秒无操作，自动写回 `0x55`，游戏内鼠标触摸恢复正常；
* **正常退出恢复**：`atexit` / Ctrl+C 都会写回 `0x55` + 空闲包；
* **断线不掉指**：手机断开时服务端会释放它按住的所有触点；
* **残留自愈**：服务端启动时若发现游戏里留着非空闲包，会先清成空闲。

如果进程被强杀（`taskkill /F`）导致游戏内鼠标触摸失效，一条命令恢复：

```powershell
python pc\maitouch_port.py restore
```

其他后端（`pc/config.json` → `touch.backend`）：

| 值 | 说明 |
| --- | --- |
| `maitouch` | **默认**，端口写入（上面这套） |
| `log` | 干跑：只打日志，不碰游戏，用于联调画面链路 |
| `synth` | 合成指针注入（`CreateSyntheticPointerDevice`）。本机实测注入成功但窗口收不到 `WM_POINTER`，**不可用**，仅作备用 |

## 画面链路

* 抓屏 `dxcam`（DXGI Desktop Duplication），只抓游戏客户区，实测 60.7 fps、16.6 ms/帧；
* 编码 **NVENC** `h264_nvenc`（`preset=p1, tune=ull, rc=cbr`），**1.4 ms/帧**，BGRA 直接进编码器由显卡转 NV12；
  无 NVENC 自动回落 `libx264`；
* 传输 WebRTC（SRTP/UDP），H.264 constrained baseline；
* 不传声音（声音留在 PC 音箱）。

端到端实测（`python tools/selftest.py`）：**60.3 fps**、WS 控制面 RTT **0–1 ms**。

## 协议（WebSocket `/ws`）

| 方向 | 消息 |
| --- | --- |
| PC→手机 | `{"t":"welcome", panel, video, window, touch_backend}` |
| PC→手机 | `{"t":"panel", cx, cy, rx, ry, stretch, state, detected}`（面板圆锁定/丢失时主动推） |
| 手机→PC | `{"t":"offer","sdp":...}` → PC 回 `{"t":"answer","sdp":...}` |
| 手机→PC | `{"t":"touch","id":1..10,"a":"down|move|up","x":0..1,"y":0..1}` |
| 手机→PC | `{"t":"ping","ts":毫秒}` → `{"t":"pong",...}` |

`x,y` 是**游戏画面内的归一化坐标**（0,0 = 左上角，1,1 = 右下角），与手机分辨率/黑边无关。
`panel` 里带面板圆（`cx, cy, rx, ry`）、半径阈值与传感器标号，手机端据此画环。

## 自测（不需要手机）

```powershell
python pc\sensors.py                      # 17 传感器几何/包编解码自检（含偏心/拉缩面板）
python pc\panel_detect.py --track 10      # 实时看面板圆检测：dark → searching → locked
python tools\test_panel_detect.py         # 面板圆检测单测（合成画面，21 项）
python tools\selftest.py                  # 起服务 + 模拟手机，验证画面链路（PASS/FAIL）
python tools\test_encoder.py              # NVENC 编码 + 解码回环
python tools\test_port_backend.py         # 游戏运行时：端口写入路径 12 项检查
python tools\test_e2e_touch.py --url 127.0.0.1:8770   # 手机→PC→游戏内存 端到端触摸验证
python tools\test_web_grid.py dump && node tools\test_web_grid.js   # 手机端传感器数学 vs PC 端
python tools\bench_video.py               # 抓屏/编码基准
```

`test_e2e_touch.py` 会先向服务端要面板圆，再从**另一个进程**读游戏内存来核对：按中心/外环/内环/圆外、
双指同按、松手回空闲、断线自动松手 —— 本项目实测 14/14 通过（触摸坐标全部按检测到的圆换算）。

## 常见问题

* **连不上**：确认手机与 PC 同网段；Windows 防火墙放行 `python.exe`（或 `8770` TCP）。
* **手机上的环和游戏里的圆不重合**：先看 `/api/panel` 的 `state` 是不是 `locked`；
  不是的话让游戏停在待机画面（黑屏结束后的画面，左下角有大圆）几秒，服务端会自动锁定。
  仍不行就 `python tools\panel_preview.py` 生成 `tools\probe_out\panel_overlay.png` 看检测对不对，
  然后用上面的 `panel.manual` 手工固定。
* **画面上有两个一样的圆**：待机画面左右各有一个（两个玩家画面并排），服务端只用**左下角**那个；
  要驱动另一侧需要另行扩展（见 `docs/architecture.md` §9）。
* **画面有、触摸没反应**：看 `http://<PC-IP>:8770/api/status` 的 `game.touch_details`
  （`attached` / `frozen` / `packet` / `sensors`）与 `game.touch.errors`。
* **游戏里鼠标点触摸面板失效**：说明 `build_status` 还处于冻结，5 秒后自动恢复；
  急用就 `python pc\maitouch_port.py restore`。
* **卡顿/掉帧**：`pc/config.json` 里降 `video.bitrate_kbps`（4000–6000）或 `video.fps`（30）。
* **抓到的不是游戏**：窗口类名 `teaGfx`、标题 `Micetools`；被遮挡不影响抓屏。

## 已知边界

* **游戏内判定未做最终闭环验证**：已证明（a）我们写入的就是设备线程推给游戏的那块 buffer
  （`port_write` 逐字节比对）、（b）游戏在该状态下确实在向设备发 `{STAT}` 要触摸数据、
  （c）游戏自己的鼠标路径写的也是这块 buffer（按住鼠标中心 → 包里出现 `C` 位）。
  至于「按住某传感器 → 游戏内出现判定」，需要有画面才能看，见 `docs/re_maitouch.md` §9。
  手动验证最省事：游戏里 `python pc\maitouch_port.py set C` 然后看画面/声音。
* 17 个传感器 → maimai 自己 zone 命名（A1..E?）的展开由游戏决定，本次未逆向游戏本体，
  因此手机端标号是「屏幕方位」标号，可能与游戏内的 zone 名不完全对应。
* 只支持这一套 micetools/mice.dll 版本：`build_status` 前导不是 `55 8B EC 83 E4 F8` 时会拒绝打补丁并报错。
* **面板圆检测依赖游戏画的那个圆**：换了游戏版本/分辨率以后，`/api/panel` 的 `state` 会退回
  `searching`（或 `full`），此时触摸按整幅画面换算，环的位置会和游戏不一致 —— 用 `panel.manual` 固定，
  或调 `pc/config.json` 的 `panel.roi`（默认 `[0.0, 0.30, 0.55, 1.0]`，即左下角）。

## 免责 / 边界

本项目只做「显示 + 触摸转发」，不修改磁盘上的任何游戏文件、不做鼠标模拟。
对 `mice.dll`/`maimai*.exe` 的所有操作都是**运行时只读 + 一个 1 字节补丁**，进程退出即消失。
