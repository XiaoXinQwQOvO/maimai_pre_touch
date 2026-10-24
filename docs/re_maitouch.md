# maitouch 虚拟串口逆向报告

> 对象：`G:\Original\maimai\mice.dll`（micetools 的 maimai 触摸模拟组件），
> 目标游戏：`G:\Original\maimai\maimai_dump_.exe`（`mice.exe` 启动器注入 `mice.dll`）
> 本文所有地址均为 **mice.dll 模块内 RVA**；文件映像基址 `0x10000000`（PE
> OptionalHeader.ImageBase，`/DYNAMICBASE` 关闭），**运行时实际加载基址每次由启动器
> 决定，实测两次为 `0x69690000`**，所以代码/数据一律用 `模块基址 + RVA`，不要硬编码 VA。
> 全部脚本在 `tools/`，原始证据 JSON 在 `tools/probe_out/`。

---

## 0. 结论摘要

| # | 问题 | 结论 | 证据 |
|---|---|---|---|
| 1 | maitouch 回复（设备→游戏）的精确格式 | **14 字节定长**：`'(' (0x28)` + 12 个传感器字节 + `')' (0x29)`；每个传感器字节 = `0x40 \| 位掩码`，空闲全是 `0x40`。只用到第 1–4 号传感器字节（17 个传感器：中心 1 + 内圈 8 + 外圈 8），5–12 号恒为 `0x40` | §3.1、§6 |
| 1b | 游戏→设备的命令 | 6 字符帧 `{`+5 字节：`{HALT}` / `{STAT}` / `{<a><sensor>k<value>}`（阈值下发）/ `{<a><sensor>th}` | §3.2 |
| 2 | 谁在填这些字节 | **只有鼠标**：`GetAsyncKeyState(VK_LBUTTON)` + `GetCursorPos` + `ScreenToClient`，单点。WM_POINTER 路径代码存在但本机收不到任何 pointer 消息；**没有任何其他来源** | §4、§5 |
| 3 | 合成触摸能否改变触摸状态 | **不能**。`CreateSyntheticPointerDevice` + `InjectSyntheticPointerInput` 返回成功，但系统不向窗口投递 WM_POINTER（普通窗口对照实验 0 条 WM_POINTER，鼠标对照有 4 类消息）→ 路线 (a) 不可行 | §5 |
| 4 | 可外部控制的写入通道 | **路线 (b) 已验证**：把 `build_status`（RVA `0x43FD`）首字节改成 `ret`，再用 `WriteProcessMemory` 直接写 14 字节状态包到 `模块基址+0x18B040`。驱动层（`port_write` 钩子）与游戏侧读取（`port_read` 钩子）都回读到我们写入的字节 | §7 |

**推荐给产品用的接口**：`tools/maitouch_state.py` 的 `MaitouchWriter`（纯 ctypes，无 frida 依赖），
或 `tools/maitouch_ctl.py daemon`（UDP 命令服务）。

---

## 1. 静态分析基础

PE 结构（`tools/re_pe.py` 解析）：

| section | RVA | 说明 |
|---|---|---|
| `.text` | `0x1000` | 代码 |
| `.rdata` | `0x102000` | 字符串、浮点常量 |
| `.data` | `0x15f000` | 全局变量（含状态包缓冲、触摸列表） |
| `.detourc` | `0x196000` | 注入用的 trampoline 区 |
| `.reloc` | `0x199000` | 重定位 |

关键 RVA 表（全部来自静态反汇编 + 运行时钩子双向确认）：

| RVA | 符号（本文命名） | 说明 |
|---|---|---|
| `0x44FA` | `maitouch_device_loop` | maitouch 虚拟串口设备线程主循环 |
| `0x43FD` | `build_status` | 生成 14 字节状态包（**外部写入方案要冻结它**） |
| `0x431B` | `add_point(W,H,px,py)` | 客户端像素 → 传感器位；返回 0 表示不落区 |
| `0x41DE` | `zone_encode` | 归一化坐标 → 象限/环/位（被 `add_point` 调用） |
| `0x41C4` | `sensor_char_to_index` | 命令里的传感器字母 → 索引（0..19，跳过 4/9/14） |
| `0x11793` | `port_write(this,buf,n)` | **设备→游戏**方向写（含 `0xD0` 转义） |
| `0x1175E` | `port_read(this,buf,n)` | **游戏→设备**方向读（含反转义） |
| `0x117D9` | `port_avail()` | FIFO 中可读字节数 |
| `0x18D4E` / `0x18E05` | `ring_write` / `ring_read` | 每设备 2 KiB 环形 FIFO（`dev+0x800` 读指针、`dev+0x802` 写指针，掩码 `0x7ff`） |
| `0x8DCD` | `contact_upsert(id,x,y,incontact)` | WM_POINTER 联系人表增删改 |
| `0x8ED8` | `on_pointer(...)` | WM_POINTER 消息处理（要求 `GetPointerType()==2` 即 PT_TOUCH） |
| `0x8FC1` | `subclass_wndproc` | 游戏窗口子类化过程，处理 `0x241-0x243, 0x246-0x247, 0x249-0x24C` |
| `0x90B8` | `install_subclass(hwnd)` | 记录 `g_hwnd` + `SetWindowSubclass` |

关键全局变量：

| VA | 类型 | 含义 |
|---|---|---|
| `0x18B040` | `uint8[14]` | **状态包缓冲（外部写入点）** |
| `0x161248` | `uint32` | touch active 标志（`{STAT}` 置 1，`{HALT}` 清 0） |
| `0x1614E0` | 向量容器 | WM_POINTER 联系人表（`+4` count，`+8` stride，`+0xC` data） |
| `0x1614E4/0x1614E8/0x1614EC` | `u32` | 联系人表 count / stride(=12) / data 指针 |
| `0x18AAC0` | `HWND` | 游戏窗口句柄（用于 `GetWindowRect`/`ScreenToClient`） |
| `0x18AA70` | `uint8` | 最近一条 `k` 命令的参数值 |

关键 rdata 字符串：`maitouch` `0x13198C`、`(    )` `0x1318A8`、`HALT}` `0x1318B0`、
`STAT}` `0x1318F8`、`Touchscreen entered/left/already/not in active mode` `0x131900` 起、
`k-command recieved: %d >=%d` `0x131944`、`th-command recieved: %d` `0x131960`、
`Unhandled: {%.*s` `0x131978`、传感器字母表 `0x1319A0`
（`"ABCD\0EFGH\0IJKL\0MNOPQRSTU\0VWXY"`，共 31 字节，`0x41C4` 扫描它做字母→索引）、
`15222` `0x1319C0`、`Micetools` `0x1319C8`。

浮点常量（`tools/re_query.py raw` 读出）：

| VA | 值 | 用途 |
|---|---|---|
| `0x150744` | `0.05020919` | 中心区半径²（r < 0.2241） |
| `0x150770` | `0.3045405` | 内/外圈分界半径²（r < 0.5518） |
| `0x1507DC` | `1.0` | 有效半径²（r ≥ 1 → 不落区） |

---

## 2. 设备线程主循环 `0x44FA`

```
loop:
  if (g_touch_active /*0x161248*/ && port_avail(0x117D9) == 0) {
      build_status();                       // 0x43FD
      port_write(packet /*0x18B040*/, 14);  // 0x11793
      Sleep(2);                             // ~500 Hz
  } else {
      read 1 byte until '{';                // 0x1175E
      read 5 more bytes;
      if (frames == "{HALT}")  g_touch_active = 0;   // 日志: Touchscreen left active mode
      if (frames == "{STAT}")  g_touch_active = 1;   // 日志: Touchscreen entered active mode
      if (b[6]=='k'  && b[8]=='}') { sensor = sensor_char_to_index(b[5]); [0x18AA70] = b[7]; ... }
      if (b[6]=='t'  && b[7]=='h' && b[8]=='}') { sensor = sensor_char_to_index(b[5]); ... }
      else log("Unhandled: {%.*s");
  }
```

实测速率：idle 相位 1 秒内 `build_status` 335 次、`port_write(14)` 335 次；
另一段 720 次/秒。即 **每 1.4–3 ms 推一帧**。

---

## 3. 协议

### 3.1 设备 → 游戏：14 字节状态包（已完全验证）

```
偏移  0     1     2     3     4     5 .. 12   13
      '('   sensor1 ...                sensor4  ')'   (sensor5..12 恒 0x40)
0x28  0x40|bits ...                      0x40|bits 0x29
```

* 空闲包（实测，反复出现）：`28 40 40 40 40 40 40 40 40 40 40 40 40 29`
* 全 17 区按下（外部写入实验）：`28 4F 4F 4F 5F 40 40 40 40 40 40 40 40 29`
* `0x40`（字符 `@`）是"无触摸"基值，位掩码直接 OR 上去。
* 只有第 1–4 号传感器字节会被赋值：17 个传感器 = 4×4 + 1（中心 `byte4 bit 0x10`）。
  第 5–12 号字节（合计 8 字节）在本实现中**恒为 0x40**，属于协议预留。
* 写方向带 `0xD0` 转义（`0xD0 → D0 CF`，`0xE0 → D0 DF`），但状态包字节范围
  `0x28/0x40-0x58/0x29` 不含 `0xD0/0xE0`，**实际不会触发转义**。

### 3.2 游戏 → 设备：6 字符命令帧

读侧：先逐字节读到 `'{'`，再收 5 字节；即命令总长 6。

| 命令 | 含义 | 实测/日志证据 |
|---|---|---|
| `{STAT}` | 进入触摸主动模式（开始推状态包） | mice 日志 `Touchscreen entered active mode` |
| `{HALT}` | 退出主动模式 | `Touchscreen left active mode` |
| `{<a><S>k<v>}` | 逐传感器阈值下发，`<S>` 是传感器字母，`<v>` 是值 | 日志 `k-command recieved: %d >=%d`，启动时连续下发 17 组（索引 0..19，跳过 4/9/14 → 正好 A..Q 17 个传感器），值如 150 |
| `{<a><S>th}` | 另一类逐传感器命令（无值字节） | 日志 `th-command recieved: %d` |

传感器字母表在 `0x1319A0`：`A B C D \0 E F G H \0 I J K L \0 M N O P Q R S T U \0 V W X Y`
→ 索引 `0..3`、`5..8`、`10..13`、`15..23`、`25..28`；maitouch 实际只用 `0..19` 去掉 4/9/14，
**正好 17 个**（A..Q），与 17 个传感器一一对应。

> 注意：这些命令**只在启动握手阶段**出现一次。我们用 frida 在游戏进程创建后 ~0.3 s 内挂上，
> 仍然只抓到 idle 阶段的 6 次单字节读（内容 `e0 ff 03 f0 …`，不是 `{` 开头的帧），
> 说明握手命令发生在 mice.dll 设备线程启动的极早期（或由 `mice.exe` 侧完成）。
> 命令格式以静态反汇编 + mice 日志为准；要对拍可把 `tools/frida_capture_cmds.py`
> 的 attach 提前到 `frida.spawn` 阶段。

### 3.3 端口对象与缓冲

* 每个虚拟设备对象 `dev` 有 2 KiB 环形 FIFO：读指针 `dev+0x800`、写指针 `dev+0x802`、掩码 `0x7FF`。
* `port_write` → `ring_write(dev+0x800)`；`port_read` ← `ring_read`（先反转义再交给调用者）。
* 设备线程是**主动推送**（约 500 Hz），游戏侧 `ReadFile` 只是把 FIFO 里累积的字节取走；
  所以外部只要把包"放对地方"，游戏下一次轮询就能读到。
* `ReadFile` 由 mice 接管（`0x84B6` 是通用 mock，`dev+0x10` 才是设备专属读处理），
  因此 hook `kernel32!ReadFile` **看不到** COM3 的读取，必须 hook mice 自己的函数（`0x1175E`）。

---

## 4. 触摸状态的来源（问题 2）

### 4.1 `build_status`（`0x43FD`）逐步逻辑

```c
build_status():
    memset(packet+1, 0x40, 12);            // 全部清零成 "无触摸"
    hwnd = g_hwnd;                         // [0x18AAC0]
    if (hwnd) {
        GetWindowRect(hwnd, &rc);          // [IAT 0x102364]
        if (GetAsyncKeyState(VK_LBUTTON)) {// [IAT 0x10236C]，只看左键是否按下
            GetCursorPos(&pt);             // [IAT 0x102370]
            ScreenToClient(hwnd, &pt);     // [IAT 0x102368]
            add_point(W, H, pt.x, pt.y);   // 取窗口矩形宽高做归一化
        }
    }
    for (each contact in 联系人表)          // count[0x1614E4] stride[0x1614E8]=12 data[0x1614EC]
        add_point(W, H, contact.x, contact.y);
    // 返回值写进 packet: packet[idx] |= bit
}
```

**只有两个输入源**：左键光标（单点）和 WM_POINTER 联系人表（最多 10 点）。
没有配置文件、没有网络、没有共享内存输入 —— 也就是说 mice 本身**没有**为触摸提供任何外部注入接口，
这就是为什么必须走后文 §7 的路线 (b)。

### 4.2 鼠标路径实测（网格扫描，`tools/frida_mouse_grid.py`）

按住左键，把光标扫过 960×960 客户区 20×20 网格（步长 48 px），frida 记录
`add_point` 每次被调用时的 `(W,H,px,py)` 以及被 OR 进包里的字节：

```
[i] addpoint calls=5079   distinct WxH={'960x960': 5079}   keys=324
```

* **W,H 实测 = 960×960**（客户区尺寸，不是 966×989 的窗口矩形；见 §6 讨论）。
* 归一化 `u = 2*px/W - 1`，`v = 2*py/H - 1`（`add_point` 里 `divss` 之后进 `zone_encode`）。
* 部分样本（deg 为 `atan2(v,u)` 的角度，0° = 屏幕右，顺时针为正）：

| 客户端像素 | u | v | r | 角度 | 命中字节 | 位 |
|---|---|---|---|---|---|---|
| 486,486 | 0.013 | 0.013 | 0.018 | 45.0° | 4 | 0x10 |
| 582,486 | 0.213 | 0.013 | 0.213 | 3.4° | 4 | 0x10 |
| 582,534 | 0.213 | 0.113 | 0.240 | 27.9° | 2 | 0x02 |
| 918,486 | 0.912 | 0.013 | 0.913 | 0.8° | 2 | 0x01 |
| 774,774 | 0.613 | 0.613 | 0.866 | 45.0° | 2 | 0x04 |
| 342,294 | −0.287 | −0.388 | 0.482 | 233.4° | 4 | 0x08 |
| 6,534 | −0.988 | 0.113 | 0.994 | 173.5° | 3 | 0x04 |
| 6,390 | −0.988 | −0.188 | 1.005 | 190.8° | — | 不落区 |

（完整 323 行见 `tools/probe_out/mouse_grid.json`，用 `tools/analyze_grid.py` 复算。）

### 4.3 WM_POINTER 路径

代码齐全（`0x8FC1` 子类化过程 + `0x8ED8` 消息处理 + `0x8DCD` 联系人表），但运行期
对 `0x8FC1` 的钩子只记录到：`0x84(WM_NCHITTEST) 791 次、0x200(WM_MOUSEMOVE) 285 次、
0x201/0x202/0x215/0x46/0x47/0x7/0x86/0x282/0x288…`，**一条 `0x246 (WM_POINTERDOWN)` 都没有**。

---

## 5. 合成触摸注入（问题 3）—— 路线 (a) 判定：不可行

### 5.1 三次独立实验

| 实验 | 做法 | 结果 |
|---|---|---|
| 游戏内 | frida 钩 `add_point`/`contact_upsert`，用 `CreateSyntheticPointerDevice`+`InjectSyntheticPointerInput` 在游戏窗口上按下/移动/抬起 | `addpoint=0`、`contacts_add=0`、联系人表 count=0、所有状态包仍是空闲包 |
| 普通 Win32 窗口对照 | 自建窗口 + 完整 `WNDPROC`（修正 ctypes 类型后），先鼠标对照再合成触摸 | 鼠标：`WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP/WM_NCHITTEST` 齐全；合成触摸：**0 条 WM_POINTER** |
| 系统能力探测 | `GetSystemMetrics(SM_DIGITIZER)=0xC5`、`SM_MAXIMUMTOUCHES=10`、`GetPointerDevices()`=2 个（type 3 maxcontacts 10、type 1 maxcontacts 1） | 系统自认为有触摸能力，`InjectSyntheticPointerInput` 也返回成功，但**消息不投递** |

结论：在本机（远程会话/无真实触摸硬件）上，`InjectSyntheticPointerInput` 的注入不产生
任何窗口消息，游戏侧拿不到 pointer ⇒ **路线 (a) 不可用**，不必再尝试其它合成触摸 API
（它们走同一条 Win32 输入通道）。

### 5.2 由此得到的可用输入面

只有鼠标（单点）。maimai 需要多点（双押、长条同时按）⇒ 必须直接改状态包 ⇒ 路线 (b)。

---

## 6. 传感器几何（实测标定）

`zone_encode`（`0x41DE`）的分区规则（静态 + 实测完全一致）：

```
u = 2*px/W - 1 ,  v = 2*py/H - 1 ,  r² = u² + v²
r ≥ 1.0            -> 不落区（返回 0）
r < 0.2241         -> 中心传感器：byte4 |= 0x10
否则按 atan2(v,u) 的 45° 八分区 + 两个环：
   象限 A1: u>0, v<0      bit = (u > |v|) ? 0x04 : 0x01 , byte 1
   象限 A2: u>0, v>0      bit = (u >  v ) ? 0x01 : 0x04 , byte 2
   象限 B1: u<0, v>0      bit = (v > |u|) ? 0x01 : 0x04 , byte 3
   象限 B2: u<0, v<0      bit = (|u|>|v|) ? 0x01 : 0x04 , byte 4
   r < 0.5518（内圈）时 bit <<= 1（0x01→0x02, 0x04→0x08）
```

即 **0x01 = 该象限靠垂直轴的那 45°，0x04 = 靠水平轴的那 45°**，内圈左移一位。

几何参数（px，客户区 960×960）：中心半径 107.6 px，内/外圈分界 264.9 px，外圈边界 480 px。
圆心 = 客户区 (480,480) = 屏幕 (1280,720)（窗口矩形 `(797,214)-(966,989)`，客户区原点 `(800,240)`）。

### 17 个传感器完整表（外部写入用的位表）

| zone | 环/方位 | 包内字节 | 位 | 客户区 px（代表性） | 屏幕 px |
|---|---|---|---|---|---|
| C | 中心 | 4 | 0x10 | 480,480 | 1280,720 |
| O0 | 外圈 0–45° | 2 | 0x01 | 835,627 | 1635,867 |
| O45 | 外圈 45–90° | 2 | 0x04 | 627,835 | 1427,1075 |
| O90 | 外圈 90–135° | 3 | 0x01 | 333,835 | 1133,1075 |
| O135 | 外圈 135–180° | 3 | 0x04 | 125,627 | 925,867 |
| O180 | 外圈 180–225° | 4 | 0x01 | 125,333 | 925,573 |
| O225 | 外圈 225–270° | 4 | 0x04 | 333,125 | 1133,365 |
| O270 | 外圈 270–315° | 1 | 0x01 | 627,125 | 1427,365 |
| O315 | 外圈 315–360° | 1 | 0x04 | 835,333 | 1635,573 |
| I0 | 内圈 0–45° | 2 | 0x02 | 657,553 | 1457,793 |
| I45 | 内圈 45–90° | 2 | 0x08 | 553,657 | 1353,897 |
| I90 | 内圈 90–135° | 3 | 0x02 | 407,657 | 1207,897 |
| I135 | 内圈 135–180° | 3 | 0x08 | 303,553 | 1103,793 |
| I180 | 内圈 180–225° | 4 | 0x02 | 303,407 | 1103,647 |
| I225 | 内圈 225–270° | 4 | 0x08 | 407,303 | 1207,543 |
| I270 | 内圈 270–315° | 1 | 0x02 | 553,303 | 1353,543 |
| I315 | 内圈 315–360° | 1 | 0x08 | 657,407 | 1457,647 |

（`python tools/print_zone_table.py` 可复现；`maitouch_state.zone_at(cx,cy)` / `zone_center(name)`
做坐标↔传感器双向换算，自检 17/17 通过。）

**已验证 vs 推断**：

* 已验证：字节/位表、17 个传感器的存在、环阈值、八分区规则、`0x40` 基值、
  第 5–12 号字节不参与 —— 这些都由 323 个鼠标采样点 + 静态常量交叉验证；
* 推断（弱）：传感器字母 A..Q 与 (byte,bit) 的**顺序对应**（假设位序 = 字母序）；
  这不影响写入通道（写入只需位表），只影响 `k` 命令语义。
* 未验证：游戏如何把 17 个传感器展开成它自己的 zone（maimai 侧逻辑，未在本次范围内）。

---

## 7. 可外部控制的写入通道（路线 b，已端到端验证）

### 7.1 原理

1. `build_status`（RVA `0x43FD`，序言 `55 8B EC 83 E4 F8`）是**唯一**会重写状态包的地方，
   且它在每次推包前都会把包整体清成空闲值；
2. 把首字节 `0x55` 改成 `0xC3`（`ret`）⇒ 该函数立刻返回，状态包再也没人改；
3. 用 `WriteProcessMemory` 把 14 字节写到 `模块基址+0x18B040`；
4. 设备线程（~500 Hz）照旧把这块缓冲经 `port_write` 推给游戏 ⇒ 游戏看到的触摸状态完全由我们决定。

一次写入即可长期保持（不需要高速覆写、没有竞态）。

### 7.2 代码

```python
import maitouch_state as MS           # tools/maitouch_state.py

w = MS.MaitouchWriter()               # 自动找 maimai_dump_.exe + mice.dll 基址
print(w.probe())                      # {'base': '0x69690000', 'build_status': '55 8b ec 83 e4 f8', ...}
w.freeze()                            # 打 ret 补丁（幂等，会校验原字节）
w.write_packet(MS.packet_from_zones(["C", "O0", "I90"]))   # 多点同时
# ... 需要改就再写一次 ...
w.restore()                           # 还原 0x55 + 写回空闲包
```

命令行（`tools/maitouch_ctl.py`）：

```
python tools/maitouch_ctl.py probe
python tools/maitouch_ctl.py set C O90 I270
python tools/maitouch_ctl.py raw 2:0x41 4:0x50
python tools/maitouch_ctl.py clear
python tools/maitouch_ctl.py restore
python tools/maitouch_ctl.py daemon --port 5599     # UDP: PING/SET/RAW/CLEAR/GET/RESTORE
```

### 7.3 验证数据（`tools/frida_verify_channel.py` → `tools/probe_out/verify_channel.json`）

在游戏进程里挂 `port_write`（RVA `0x11793`）钩子，逐帧比对"我们写的包"与"驱动真正交给游戏的包"：

```
[i] probe: base=0x69690000, build_status='55 8b ec 83 e4 f8', packet=28 40 ... 29
[i] freeze: frozen       -> 序言变成 c3 8b ec 83 e4 f8
[i] single zone O0 written 2840414040404040404040404029 -> wire ['2840414040404040404040404029']  (build_status 调用 0 次)
[i] multi ['C','O0','I90','O180','I270','O45'] -> 2842454251404040404040404029 | wire 同上（逐字节相同）
[i] all zones -> 284f4f4f5f404040404040404029 | wire 同上
[i] cleared -> wire 2840404040404040404040404029
[i] after restore -> wire 2840404040404040404040404029
```

* 冻结后 `build_status` 调用数 = 0 → 证明没有第二个写者，包不会被覆盖；
* 每一次写入都**逐字节**出现在 `port_write` 的 14 字节负载里；
* 还原后包回到空闲值，说明可回滚。

游戏侧读取验证见 `tools/frida_readfile_proof.py`（钩 `port_read` RVA `0x1175E`，
它是游戏 `ReadFile` 最终落到 mice 的设备读处理）→ `tools/probe_out/readfile_proof.json`。

> **已知边界**：本轮在**街机待机/演示画面**下做验证，画面像素差对比没有看到游戏内反应
>（`before_vs_hold` 与 `before_vs_after` 的 mean 差都是 2.19，属于同一级动画噪声），
> 因此"游戏**逻辑上**消费了这帧"这一步只有驱动层/读取层证据，没有"游戏内可观察行为"证据。
> 需要在**真正的游戏画面**（例如 touch 测试界面或在玩状态）里做一次按下-保持验证，
> 见 §9 待办。

### 7.4 时序要求

| 项 | 值 |
|---|---|
| 设备推包周期 | `Sleep(2)` ≈ 2 ms（实测 335–720 帧/秒） |
| 冻结后写入生效延迟 | ≤ 1 个推包周期（≤3 ms），无竞态 |
| 状态保持 | 无限期（冻结后无人重写） |
| 恢复 | 写回 `0x55` 后 1 帧内恢复由鼠标驱动 |
| 建议刷新策略 | 变化时写一次即可；若做保险可每 100 ms 重写一次（成本 ~ 1 µs） |

### 7.5 生产化建议

* **不要**依赖 frida：`MaitouchWriter` 只用 `OpenProcess` + `Toolhelp32` + `WriteProcessMemory`，
  已实测可用（frida 只在验证阶段用；注意 frida 的 `Interceptor.attach(0x43FD)` 会用 `jmp`
  改写序言，导致外部 `ret` 补丁校验失败 —— 这是我们在验证脚本里分开两个 agent 的原因）。
* 若要免去"外部进程"依赖，可走 `mice.ini` 的注入机制做一个小 DLL：在 `DllMain` 里
  找 `mice.dll` 基址后做同样两件事（补丁 `+0x43FD`、持有 `+0x18B040`），
  再用 UDP/共享内存接收指令；由于 mice 已在目标进程内，DLL 无需再 `OpenProcess`。
* 找不到进程/模块时给出明确错误；游戏重启后需重新 `freeze()`（基址可能变化）。

### 7.6 失败模式

| 场景 | 表现 | 处理 |
|---|---|---|
| 游戏未启动 / 进程名不同 | `find_pid()` 返回 None | 轮询等待，提示用户 |
| `mice.dll` 未加载 | `module_base()` 抛错 | 等 `mice.dll` 出现（启动后数秒） |
| 序言不是 `0x55` | `freeze()` 抛 `RuntimeError` | 说明 frida 仍在挂钩或 mice.dll 版本不同 → 先 detach/换版本适配 |
| 游戏重启 | 补丁失效、基址变化、包缓冲重置 | 重新获取 base 再 `freeze()` + 写包 |
| 服务先于游戏启动 | 首次指令失败 | 失败后自动重试（daemon 已实现：出错即丢弃句柄，下次指令重连） |
| 补丁未打就写包 | 包被 `build_status` 立刻覆盖 | 必须先 `freeze()`（`set/raw/clear` 都内置调用） |

---

## 8. 复现步骤

```powershell
# 0) 环境
$env:PYTHONIOENCODING='utf-8'
pip install frida capstone            # 仅验证脚本需要

# 1) 静态分析（不启动游戏）
python tools\re_pe.py sections        # 段表
python tools\re_xref_touch.py         # maitouch 字符串簇交叉引用
python tools\re_disasm.py 0x100044fa 0x10004900    # 设备主循环

# 2) 鼠标→传感器标定（会启动游戏，结束自动清理）
python tools\frida_mouse_grid.py --step 48
python tools\analyze_grid.py

# 3) 外部写入通道验证（会启动游戏，结束自动清理）
python tools\frida_verify_channel.py
python tools\frida_readfile_proof.py

# 4) 日常使用（游戏运行时，另一个进程执行）
python tools\maitouch_ctl.py probe
python tools\maitouch_ctl.py set C O0 I90
python tools\maitouch_ctl.py restore
```

清理：所有 frida 脚本结束都会 `taskkill /F` 掉 `maimai_dump_.exe / maimai.exe / mice.exe /
MovieProc.exe / ALLNetProc.exe / CameraUploader.exe / GmSync.exe`；
`maitouch_ctl.py restore` 会把 `0x55` 写回。

---

## 9. 未解决问题 / 边界

1. **游戏内可观察行为的最终确认**：本轮只在待机画面验证到驱动层与读取层。
   需要在实际游戏画面里（或 touch 测试界面）做"按住某传感器 → 游戏内出现触摸判定"的确认。
   最省事的做法：`python tools\maitouch_ctl.py set C`，看游戏画面/日志是否响应。
2. **17 传感器 → 游戏 zone 的映射**：maimai 侧把 17 个传感器展开成它自己的 zone（含 `A1..E?` 命名），
   本次未逆向游戏本体，因此只能给出"传感器位 → 屏幕方位/环"的映射。
   若产品需要按游戏 zone 名下发，需要再逆向 `maimai_dump_.exe` 的触摸解析。
3. **传感器字母与位序的对应**（§6 推断项）：只影响 `k` 命令语义，不影响写入。
4. **握手命令帧未在运行时抓到**（§3.2 备注）：格式来自静态分析 + mice 日志；
   要实抓需在 `frida.spawn` 阶段就 attach。
5. `idle` 相位抓到 6 次单字节读（`e0 ff 03 f0 …`）不属于 maitouch 命令帧，
   怀疑是同一 `port_read` 被其它虚拟设备复用，未深究。
