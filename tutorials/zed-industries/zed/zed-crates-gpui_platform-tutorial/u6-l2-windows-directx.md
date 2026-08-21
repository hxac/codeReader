# u6-l2 WindowsPlatform 与 DirectX 渲染栈

## 1. 本讲目标

学完本讲，你应该能够：

1. 描述 Windows 平台上 GPUI 渲染栈的四层结构：**设备层（DirectXDevices）→ 渲染器层（DirectXRenderer）→ 图集层（DirectXAtlas）→ 窗口呈现（WindowsWindow）**，并说出每一层的关键类型与初始化时机。
2. 解释 DirectWrite 在 Windows 文本系统中的位置：它既是字体/整形引擎，又通过 GPU 状态参与彩色表情的光栅化，还向渲染器提供伽马与对比度参数。
3. 说明 `direct_manipulation` 如何借助 Windows 的 Direct Manipulation API，把精密触控板手势翻译成 GPUI 的 `ScrollWheelEvent` 与 `PinchEvent`。
4. 理解 Windows 平台特有的帧驱动方式：由一个独立的 `VSyncProvider` 线程在每个垂直同步周期 invalidate 所有窗口，而不是像 Wayland 那样覆写 `PlatformWindow::schedule_frame`。

本讲是第 6 单元的第二篇。上一篇（u6-l1）讲的是 macOS；本讲切换到 Windows，重点不再是「Platform trait 有哪些方法」（u2-l1 已经画过地图），而是**渲染栈如何分层组装**。

## 2. 前置知识

本讲会用到几个 Windows 图形 API 的基础概念，先用两三句话解释每一个：

- **Direct3D 11（D3D11）**：Windows 的原生 3D 图形 API。核心对象是 `ID3D11Device`（资源工厂，创建纹理/缓冲/着色器）与 `ID3D11DeviceContext`（ Immediate 模式的绘制命令录制器）。GPUI 用的是 D3D11，不是更新的 D3D12——D3D11 是立即模式（immediate mode），编程模型更简单，驱动内部再做优化。
- **DXGI**：DirectX Graphics Infrastructure，位于 D3D 之下的底层。负责枚举显卡（`IDXGIAdapter1`）与创建交换链（`IDXGISwapChain1`）。可以这样理解分工：DXGI 管「有哪些 GPU、画到哪里」，D3D 管「怎么画」。
- **交换链（swap chain）**：一块由 GPU 管理的双/三缓冲队列。渲染器画到后台缓冲，调用 `Present` 后与前台缓冲交换，屏幕因此刷新。缓冲数 `BUFFER_COUNT = 3`。
- **DirectComposition（DComp）**：系统合成器（DWM）提供的可编程合成接口。把交换链内容挂到一个 visual 树上，由 DWM 统一合成，支持预乘 alpha 的透明窗口。
- **DirectWrite**：Windows 的现代文本排版引擎，负责字体枚举、整形（shaping）、布局。Zed 在 Windows 上的整套文本系统都建在它之上（对应 gpui 的 `PlatformTextSystem` 契约，u8-l1 会展开）。
- **Direct Manipulation（DM）**：Windows 8 引入的手势识别框架，输入触摸/精密触控板数据，输出带惯性的「内容变换」（平移 + 缩放）。浏览器和开始菜单的丝滑滚动就是它驱动的。
- **etagere**：一个纹理图集装箱（bin-packing）库。它解决的问题：把大量小图（字形、图标）高效地塞进少量大纹理里，减少绘制时的状态切换。
- **消息专用窗口（message-only window）**：Windows 中一种不显示、不接收输入、只用来收消息的窗口（父句柄为 `HWND_MESSAGE`）。GPUI 用它做平台级消息枢纽。

另外回忆 u2-l1 的结论：`Platform` 契约在 gpui 主 crate，`gpui_windows` 是实现方。Windows 分支在 [gpui_platform.rs:L63-L69](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_platform/src/gpui_platform.rs#L63-L69) 中用 `.expect()` 做**启动期 fail-fast**——如果 DirectX 设备创建失败，进程直接带错误信息退出，而不是带着一个坏平台继续跑。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `gpui_windows/src/platform.rs` | `WindowsPlatform` 平台外壳 | 渲染栈三件套的出生地；VSync 线程；GPU 设备丢失恢复 |
| `gpui_windows/src/directx_devices.rs` | 设备层 | 选卡、建 D3D11 设备、恢复重试 |
| `gpui_windows/src/directx_renderer.rs` | 渲染器层 | 交换链、八条管线、一帧的绘制流程 |
| `gpui_windows/src/directx_atlas.rs` | 图集层 | `PlatformAtlas` 契约实现、etagere 装箱、纹理上传 |
| `gpui_windows/src/vsync.rs` | 垂直同步 | `DwmFlush` 等待与异常兜底 |
| `gpui_windows/src/direct_manipulation.rs` | 触控板手势 | 手势分类与 `PlatformInput` 翻译 |
| `gpui_windows/src/window.rs` | `WindowsWindow` | 渲染器与 DM 处理器的组装点（`PlatformWindow` 实现） |
| `gpui_windows/src/wrapper.rs` | `SafeHwnd`/`SafeCursor` | 让 HWND 可跨线程共享的小包装 |
| `gpui_windows/src/direct_write.rs` | 文本系统 | 与设备层的耦合点（`GPUState`） |
| `gpui_windows/src/events.rs` | 消息处理 | DM 事件泵的调用位置 |

## 4. 核心概念与源码讲解

### 4.1 WindowsPlatform：平台外壳与帧驱动线程

#### 4.1.1 概念说明

`WindowsPlatform` 是 `Platform` 契约在 Windows 上的实现。与本讲主题相关的是它做的三件事：

1. **渲染栈的出生地**：`WindowsPlatform::new` 在启动时创建 `DirectXDevices` 与 `DirectWriteTextSystem`（headless 模式则跳过，文本系统换成 `NoopTextSystem`），设备对象存在 `WindowsPlatformState` 里，之后每个新窗口从 `generate_creation_info` 里**克隆**一份带走。
2. **平台消息枢纽**：它创建一个消息专用窗口，专用窗口的窗口过程（window procedure）在 `WM_NCCREATE` 时反手构造出 `WindowsPlatformInner` 与 `WindowsDispatcher`——这是 Windows GUI「控制流倒置」的标准玩法：你无法在创建窗口前拿到 HWND，所以只能在窗口过程里完成初始化。
3. **帧驱动**：`run()` 启动一个 `VSyncProvider` 线程，每个垂直同步周期对**所有**窗口调用 `RedrawWindow(RDW_INVALIDATE)`，触发 `WM_PAINT` → `draw_window` → `renderer.draw`。这就是 Windows 版的「请求下一帧」机制。

第 3 点值得和 u5-l4 对照记忆：Wayland 上 GPUI 覆写了 `PlatformWindow::schedule_frame`，用 `frame_ping` 唤醒停泊在 `Parked` 状态的按需渲染循环；Windows 上 `schedule_frame` 走 trait 的**默认空实现**（见 [platform.rs:L864-L865](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L864-L865)），帧驱动完全由平台自己的 vsync 线程承担——这是「平台自有帧驱动」姿态的典型代表（u3-l2 讲过 schedule_frame 的语义）。

#### 4.1.2 核心流程

启动到出帧的全流程（伪代码）：

```text
gpui_platform::current_platform(false)                  # 编译期选中 windows 分支
└─ WindowsPlatform::new(headless=false)
   ├─ OleInitialize                                     # OLE/拖放初始化
   ├─ DirectXDevices::new()                             # ← 设备层（4.2）
   ├─ DirectWriteTextSystem::new(&devices)              # ← 文本系统，吃下设备
   ├─ 注册窗口类 + CreateWindowExW(HWND_MESSAGE)         # 消息专用窗口
   │   └─ WM_NCCREATE → window_procedure
   │       ├─ WindowsDispatcher::new(main_sender, hwnd)
   │       └─ WindowsPlatformInner::new(context)         # 接管 devices
   ├─ BackgroundExecutor / ForegroundExecutor            # 从 dispatcher 派生
   └─ 记录 disable_direct_composition 环境变量
run(on_finish_launching)
├─ on_finish_launching()                                # 同步执行（u2-l2 讲过）
├─ begin_vsync_thread()                                 # ← VSyncProvider 线程
└─ GetMessageW 循环                                      # 阻塞主线程
    └─ 每个垂直同步周期：
        VSyncProvider 线程: wait_for_vsync
          → RedrawWindow(每个窗口, RDW_INVALIDATE)
          → 主线程收到 WM_PAINT → draw_window → renderer.draw
```

#### 4.1.3 源码精读

先看结构体本身。`WindowsPlatform` 持有的渲染相关字段：

[platform.rs:L34-L54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L34-L54)——`WindowsPlatform` 把不可变的执行器/文本系统放在自己身上，把可变状态（包括 `directx_devices`）放进 `Rc<WindowsPlatformInner>`；`invalidate_devices` 是一个与 vsync 线程共享的 `Arc<AtomicBool>`，注释写明用途：**交换链 resize 失败时通知 vsync 线程去重建设备**。

```rust
pub struct WindowsPlatform {
    inner: Rc<WindowsPlatformInner>,
    raw_window_handles: Arc<RwLock<SmallVec<[SafeHwnd; 4]>>>,
    headless: bool,
    text_system: Arc<dyn PlatformTextSystem>,
    direct_write_text_system: Option<Arc<DirectWriteTextSystem>>,
    invalidate_devices: Arc<AtomicBool>,   // resize 失败 → 设备失效
    handle: HWND,                           // 消息专用窗口
    ...
}
```

设备与文本系统的分支创建：

[platform.rs:L114-L131](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L114-L131)——非 headless 时先建 `DirectXDevices`，再用设备构造 `DirectWriteTextSystem`。注意顺序是**先设备后文本系统**，因为 DirectWrite 的 GPU 状态（彩色表情光栅化着色器）要挂在设备上。headless 分支则完全绕开 DirectX，文本系统用 `NoopTextSystem`（呼应 u5-l2：headless 不上屏）。

消息专用窗口的创建与「窗口过程反手初始化」：

[platform.rs:L141-L175](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L141-L175)——`PlatformWindowCreateContext` 携带 sender/receiver/设备等原料塞进 `CreateWindowExW` 的 `lpCreateParams`；系统在 `WM_NCCREATE` 时把它交还给窗口过程，窗口过程随即构造 `WindowsDispatcher` 与 `WindowsPlatformInner` 并把结果写回 context，随后 `new` 从 context 里把它们取出来。`Some(HWND_MESSAGE)` 这一行就是「消息专用窗口」的标志。

[platform.rs:L1476-L1508](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L1476-L1508)——`window_procedure` 中 `WM_NCCREATE` 分支：取出 context、创建 dispatcher、构造 inner、把 `Weak<WindowsPlatformInner>` 装箱后挂到窗口的 `GWLP_USERDATA` 槽位。此后所有消息都能凭 HWND 找回 inner——这正是 u6-l1 讲过的「ivar 存 Rust 指针」模式的 Windows 版。

`run` 与帧驱动线程：

[platform.rs:L447-L465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L447-L465)——`run` 先同步执行启动回调（u2-l2 讲过各平台时机差异），再起 vsync 线程，然后进入 `GetMessageW` 循环；循环退出后触发 quit 回调。

[platform.rs:L312-L356](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L312-L356)——`begin_vsync_thread` 的循环体就是 Windows 的心跳：`wait_for_vsync()` 后先检查设备是否丢失（`check_device_lost`）或 `invalidate_devices` 标志是否被置位，需要时走 `handle_gpu_device_lost` 重建；然后对 `raw_window_handles` 里的每个窗口 `RedrawWindow(RDW_INVALIDATE)`。注意这个线程持有 `Weak` 升级检查——所有窗口销毁后线程自然退出。

vsync 的等待实现：

[vsync.rs:L25-L56](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/vsync.rs#L25-L56)——`VSyncProvider::new` 从 DWM 拿刷新周期（拿不到就回退约 60Hz 的 16_666µs），`wait_for_vsync` 调 `DwmFlush()` 阻塞到下一次合成。关键兜底：**显示器睡眠或被拔掉时 `DwmFlush` 会立即返回而不是等待**，所以用 1ms 阈值判断「等得太快」，此时改用 `Sleep(interval)`，否则 vsync 线程会退化成忙转。刷新周期按

\[ \text{interval} = \frac{\text{qpcRefreshPeriod}}{\text{QPC 每秒滴答数}} \]

换算成时长（[vsync.rs:L58-L75](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/vsync.rs#L58-L75)，还对「29µs 的离谱周期」做了二次校验，改用 `rateRefresh` 分数比率）。

跨线程共享 HWND 的安全包装：

[wrapper.rs:L27-L53](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/wrapper.rs#L27-L53)——裸 `HWND` 不是 `Send`/`Sync`，但 vsync 线程需要遍历所有窗口句柄。`SafeHwnd` 用 `unsafe impl Send/Sync` 声明「句柄只是个数值，复制到别的线程调用 Win32 API 是安全的」。这就是 wrapper.rs 的全部职责：两个小 newtype（`SafeHwnd`/`SafeCursor`），专治类型系统不放行。

#### 4.1.4 代码实践

**实践 A（源码阅读型）：追踪 GPU 设备丢失恢复链路。**

1. **实践目标**：弄清「拔掉外接显卡 / 驱动重置」之后 GPUI 如何自愈，画出消息时序。
2. **操作步骤**：
   - 从 [platform.rs:L1399-L1408](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L1399-L1408) 的 `check_device_lost`（调 `GetDeviceRemovedReason`）读起；
   - 读 [platform.rs:L1410-L1463](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L1410-L1463) 的 `handle_gpu_device_lost`：先 `sleep(350ms)` 等系统缓过来，用 `try_to_recover_from_device_lost` 重建设备（最多重试 5 次），然后向平台窗口与每个应用窗口 `SendMessageW(WM_GPUI_GPU_DEVICE_LOST)`，再等 200ms 补发 `WM_GPUI_FORCE_UPDATE_WINDOW`；
   - 追主线程侧：平台窗口收到消息后走 [platform.rs:L1145-L1152](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L1145-L1152) 替换 `state.directx_devices`；应用窗口走 [events.rs:L1255-L1270](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/events.rs#L1255-L1270) 调渲染器的 `handle_device_lost` 并置 `force_render_pending`。
3. **需要观察的现象**：恢复链路横跨两个线程（vsync 线程发 `SendMessageW` 同步等待主线程处理）、涉及两个自定义消息常量、有一次 350ms + 一次 200ms 的人为延迟。
4. **预期结果**：一张「vsync 线程 → SendMessageW → 主线程窗口过程 → 渲染器/图集重建 → 强制重绘」的时序图。
5. 实际运行验证（如外接显卡热拔）属破坏性操作，**待本地验证**。

**实践 B（运行观察型）：DirectComposition 开关。**

1. **实践目标**：体会 DComp 路径与 HWND 交换链路径的差异。
2. **操作步骤**：在 Windows 上运行任意 GPUI 示例（例如 `cargo run -p gpui --example window`），分别以正常方式与设置环境变量 `GPUI_DISABLE_DIRECT_COMPOSITION=1`（常量定义见 [directx_renderer.rs:L26](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L26)）各运行一次。
3. **需要观察的现象**：日志中出现 "Direct Composition is disabled."；窗口透明效果（若示例有 `WindowBackgroundAppearance::Blurred` 之类设置）在两模式下的呈现差异。
4. **预期结果**：禁用 DComp 后交换链改走 `CreateSwapChainForHwnd`（见 4.3.3），透明窗口能力受损但兼容性更好。**待本地验证**（本讲义作者环境为 Linux，无法替你运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WindowsPlatform::new` 要先创建 `DirectXDevices`，再创建 `DirectWriteTextSystem`，而不是各自独立创建？

**答案**：`DirectWriteTextSystem::new` 接收 `&DirectXDevices` 并在其上构造 `GPUState`（见 [direct_write.rs:L166-L179](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_write.rs#L166-L179)），因为彩色表情（emoji）的光栅化着色器要跑在这块 GPU 设备上；文本系统还实现了 `handle_gpu_lost(&DirectXDevices)`（[direct_write.rs:L221-L223](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_write.rs#L221-L223)），设备重建后 GPU 状态也要跟着重建。所以设备是文本系统的依赖，顺序不可颠倒。

**练习 2**：vsync 线程为什么用 `RedrawWindow(RDW_INVALIDATE)` 间接触发绘制，而不是直接调用渲染函数？

**答案**：因为渲染必须发生在拥有窗口消息循环的主线程（D3D11 的 immediate context 不是线程安全的），而 vsync 是独立线程。`RedrawWindow` 只是标记窗口区域无效，真正的 `WM_PAINT` 会被主线程的消息循环按序分发，从而把「该画了」这个信号安全地转移到主线程——这本质上是又一种「唤醒即再投递」（u4-l2 的核心概念）。

**练习 3**：`WindowsPlatformState::directx_devices` 为什么是 `RefCell<Option<DirectXDevices>>` 而不是普通字段？

**答案**：设备对象在整个生命周期里会被**整体替换**：GPU 设备丢失恢复时，`handle_device_lost`（platform.rs:L1145）会 `take()` 掉旧设备再放入新克隆（[platform.rs:L1145-L1152](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L1145-L1152)）；同时主线程各处可能在借用它（例如 `generate_creation_info` 给新窗口克隆设备），`RefCell` 提供运行期借用检查，`Option` 表达「可能正处于丢失/重建的空窗期」。

### 4.2 directx_devices：设备层——找到一块能用的 GPU

#### 4.2.1 概念说明

`DirectXDevices` 是渲染栈的最底层，一个可 `Clone` 的四元组：显卡适配器、DXGI 工厂、D3D11 设备、设备上下文。它解决的问题是：**在一台可能有多个 GPU（核显 + 独显 + 软件渲染器）的机器上，选出第一个满足 GPUI 需求的适配器，并在它上面建好 D3D11 设备**。所有上层对象（渲染器、图集、文本系统 GPU 状态）都从这四个句柄派生。

#### 4.2.2 核心流程

```text
DirectXDevices::new()
├─ check_debug_layer_available()      # 仅 debug 构建：探测 DXGI debug 层
├─ CreateDXGIFactory2(debug?)          # DXGI 工厂
├─ get_adapter(factory)
│   └─ for adapter_index in 0..       # 逐个枚举适配器
│       ├─ GetDesc1 → log "Using GPU: {名字}"
│       └─ get_device(adapter)         # 尝试建 D3D11 设备
│           ├─ D3D11CreateDevice(feature levels 11.1/11.0/10.1, BGRA)
│           └─ CheckFeatureSupport(结构化缓冲)   # 不支持则换下一块卡
└─ log 实际拿到的 feature level
```

关键点：枚举是**逐卡尝试**，第一块建设备成功的卡胜出；「成功」不仅是 `D3D11CreateDevice` 返回 OK，还必须支持**结构化缓冲（StructuredBuffer）**——因为渲染器把每类图元的实例数据放进结构化缓冲（见 4.3），不支持就没法工作。

#### 4.2.3 源码精读

[directx_devices.rs:L38-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L38-L44)——四元组定义，`#[derive(Clone)]` 让每个窗口都能带走一份克隆（COM 接口指针的克隆是引用计数加一，不是复制设备）：

```rust
#[derive(Clone)]
pub(crate) struct DirectXDevices {
    pub(crate) adapter: IDXGIAdapter1,
    pub(crate) dxgi_factory: IDXGIFactory6,
    pub(crate) device: ID3D11Device,
    pub(crate) device_context: ID3D11DeviceContext,
}
```

[directx_devices.rs:L106-L140](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L106-L140)——`get_adapter` 的枚举循环：每块卡先打印名字，再尝试建设备；`get_device` 失败（`.log_err()` 吞掉错误返回 None）就继续下一块。循环尾部 `unreachable!()` 在实践中不会到达：`EnumAdapters` 越界枚举时返回 Err，会被 `?` 向上传播，而不是让循环自然结束。

[directx_devices.rs:L143-L194](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L143-L194)——`get_device`：`D3D11CreateDevice` 按 11.1 → 11.0 → 10.1 的顺序请求 feature level（DXGI 会向下协商到硬件支持的最高级），随后 `CheckFeatureSupport` 检查 `ComputeShaders_Plus_RawAndStructuredBuffers_Via_Shader_4_x`，不支持就返回错误、让外层换下一块卡。`D3D11_CREATE_DEVICE_BGRA_SUPPORT` 是必需 flag（最终呈现格式是 BGRA8）。

[directx_devices.rs:L24-L36](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L24-L36)——`try_to_recover_from_device_lost`：一个通用重试器，最多调 5 次工厂闭包，重试间加 100ms 起步的递增延迟。设备丢失恢复（4.1.4 实践 A）与渲染器重建都复用它。

#### 4.2.4 代码实践

**实践（运行观察型 + 源码验证）：让 Zed 告诉你它用了哪块 GPU。**

1. **实践目标**：验证设备枚举与选卡逻辑真实生效。
2. **操作步骤**：
   - 在 Windows 上以 `RUST_LOG=info cargo run -p gpui --example window` 运行示例；
   - 观察启动日志中的 `Using GPU: ...` 与 `Created device with Direct3D 11.x feature level.` 两行，分别对应 [directx_devices.rs:L118-L121](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L118-L121) 和 [directx_devices.rs:L53-L64](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L53-L64)；
   - 若机器是双显卡（混合显卡笔记本），可在 Windows 图形设置里把示例进程指定为「省电」（核显）与「高性能」（独显）分别跑一次。
3. **需要观察的现象**：两行日志的内容随指定 GPU 变化；feature level 通常稳定在 11.1 或 11.0。
4. **预期结果**：日志与 GPU 指定一致，证明「逐卡枚举、先到先得」的策略。**待本地验证**（需 Windows 双显卡环境）。
5. 若没有 Windows 环境，可做纯源码替代：统计 `get_adapter` 里失败重试的路径，回答「一块不支持结构化缓冲的卡会发生什么」。

#### 4.2.5 小练习与答案

**练习 1**：`DirectXDevices` 是 `Clone` 的，每个窗口克隆一份。克隆的是「多份设备」还是「同一设备的多份引用」？依据是什么？

**答案**：同一设备的多份引用。`IDXGIAdapter1`/`ID3D11Device` 等都是 COM 接口，Rust 的 `Clone` 只是 `AddRef` 引用计数加一；上层所有窗口实际共享同一个 D3D11 设备与 immediate context（这正是 vsync 线程能统一检查一次 `GetDeviceRemovedReason` 就知道所有窗口都受影响的原因）。

**练习 2**：为什么 `check_debug_layer_available` 在 `#[cfg(not(debug_assertions))]` 下直接返回 false？

**答案**：DXGI/D3D debug layer 是需要单独安装的开发组件且带来可观开销，release 构建永远不会用它；debug 构建也只在探测成功时才创建带 `D3D11_CREATE_DEVICE_DEBUG` flag 的设备（[directx_devices.rs:L91-L103](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_devices.rs#L91-L103)），探测失败则降级并 `log::warn!` 提示调试功能不可用。

### 4.3 directx_renderer：渲染器层——八条管线与一帧的旅程

#### 4.3.1 概念说明

`DirectXRenderer` 是渲染栈的中间层，每个窗口一个（装在 `WindowsWindowState.renderer` 里）。它向下持有 `DirectXAtlas`（图集）与设备引用，向上实现窗口需要的 `draw`/`resize`/`sprite_atlas`/`gpu_specs` 等能力。它内部又分四组对象：

| 分组 | 结构体 | 内容 | 创建时机 |
| --- | --- | --- | --- |
| 设备 | `DirectXRendererDevices` | 四元组克隆 + `IDXGIDevice`（供 DComp）+ `ID3DUserDefinedAnnotation`（调试标注） | 渲染器构造时 |
| 资源 | `DirectXResources` | 交换链、渲染目标视图、两条路径中间纹理（其一为 4x MSAA）、viewport | 渲染器构造时（1×1），resize 时重建 |
| 全局 | `DirectXGlobalElements` | 全局/批次常量缓冲、采样器 | 渲染器构造时 |
| 管线 | `DirectXRenderPipelines` | **八条** `PipelineState<T>`：shadow、quad、path_rasterization、path_sprite、underline、mono_sprites、subpixel_sprites、poly_sprites | 渲染器构造时 |

「八条管线」对应 GPUI 场景（Scene）的图元种类：阴影、矩形、路径、下划线、单色精灵（普通字形）、亚像素精灵（次像素抗锯齿字形）、多彩精灵（彩色字形/图片）——每条管线 = 一对 HLSL 顶点/像素着色器 + 一个结构化实例缓冲 + 一个混合状态。

#### 4.3.2 核心流程

一帧的旅程（`draw` 的骨架）：

```text
draw(scene, background_appearance)
├─ skip_draws? → 直接返回（设备恢复后的首帧丢弃）
├─ pre_draw(clear_color)
│   ├─ 写 GlobalParams 常量缓冲（伽马比、视口、对比度、是否 BGR）
│   ├─ ClearRenderTargetView（不透明背景清成白，否则清成全透明）
│   └─ 绑定渲染目标 / viewport / 常量缓冲
├─ upload_scene_buffers(scene)      # 各类图元实例写入结构化缓冲
├─ for batch in scene.batches():    # 按场景顺序逐批绘制
│   ├─ Shadows  / Quads / Underlines → 对应管线 draw_range
│   ├─ Paths → 两阶段：先画进 MSAA 中间纹理并 Resolve，
│   │          再作为 sprite 拷回主目标
│   ├─ MonochromeSprites / SubpixelSprites / PolychromeSprites
│   │        → 从图集取纹理视图，draw_range_with_texture
│   └─ Surfaces → 目前为空实现（直接 Ok）
└─ present()                        # Present(0, 0)：不再等 vsync
```

`Present(0, 0)` 的第一个参数是 SyncInterval=0（不等待垂直同步），因为帧节奏已经由 vsync 线程统一把控（4.1），再等一次就双重节流了。

路径（Path）两阶段渲染的原因：GPUI 的贝塞尔路径需要抗锯齿，而主渲染目标是 1x 采样；于是先把路径三角形画到 **4x MSAA** 中间纹理，`ResolveSubresource` 解析成普通纹理，再把每个路径的包围盒作为 sprite 一次性拷回主目标——「先高采样画到旁边，再降采样搬回来」。

#### 4.3.3 源码精读

渲染器主体与字段：

[directx_renderer.rs:L39-L57](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L39-L57)——`hwnd`、`atlas: Arc<DirectXAtlas>`、`devices/resources: Option<...>`（Option 是因为设备丢失时会被 `take()` 清空再重建）、`skip_draws` 标志（恢复后的首帧丢弃，因为纹理已随设备一起消失）。

[directx_renderer.rs:L86-L95](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L86-L95)——八条管线的清单；每条在 [directx_renderer.rs:L845-L914](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L845-L914) 中以「名字 + 着色器模块 + 初始缓冲容量 + 混合状态」构造，例如 mono/subpixel 精灵缓冲初始容量 512 个实例、quad 是 64、poly 是 16。

构造函数：

[directx_renderer.rs:L154-L198](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L154-L198)——`DirectXRenderer::new(hwnd, &directx_devices, disable_direct_composition)`：依序创建设备包、图集（挂在传入设备上）、资源（初始 1×1，等首个尺寸事件 resize）、全局元素、八条管线，最后按需创建 DirectComposition 并把交换链挂上去。调用点在 [window.rs:L142-L143](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/window.rs#L142-L143)（窗口构造时）。

`PlatformWindow` 契约的两个出口：

[window.rs:L999-L1008](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/window.rs#L999-L1008)——`WindowsWindow` 的 `sprite_atlas()` 直接转发 `renderer.sprite_atlas()`（返回 `Arc<dyn PlatformAtlas>`，见 [directx_renderer.rs:L200-L202](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L200-L202)），`gpu_specs()` 转发渲染器的 `gpu_specs`。这就是「渲染器层与 gpui 契约对接」的两个方法；`schedule_frame` 未被覆写，走默认空实现。

一帧主体：

[directx_renderer.rs:L330-L392](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L330-L392)——`draw`：`skip_draws` 短路；clear 颜色按 `WindowBackgroundAppearance` 二选一（Opaque → `[1.0; 4]` 白，其余 → 全透明）；随后 `match batch` 把每批图元分派给对应管线；错误上下文里附带场景各数组的长度统计（排障利器）；末尾 `present()`。可选的 `Annotation`（[directx_renderer.rs:L103-L116](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L103-L116)）把批次标签写进 GPU 调试工具的事件轨道，RAII drop 时 `EndEvent`。

路径两阶段：

[directx_renderer.rs:L524-L585](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L524-L585)——阶段一：清空 MSAA 中间纹理并绑定为渲染目标，把所有路径顶点展开成 `PathRasterizationSprite` 实例，一次 `DrawInstanced` 画完，再 `ResolveSubresource` 降采样。MSAA 采样数固定 4（[directx_renderer.rs:L28-L29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L28-L29)，注释说明 D3D11 保证支持）。

[directx_renderer.rs:L587-L629](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L587-L629)——阶段二：把路径作为 sprite 拷回主目标。有一处精妙的正确性分支：若批内所有路径 `order` 相同则包围盒互不相交，可逐个拷贝；若混合了不同 order，则取**最小包围矩形**一次拷贝，保证透明路径的每个像素只被写一次。

交换链的两种形态：

[directx_renderer.rs:L1201-L1225](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1201-L1225)——DComp 路径：`CreateSwapChainForComposition`，`DXGI_ALPHA_MODE_PREMULTIPLIED`（预乘 alpha，支持透明窗口）、`DXGI_SCALING_STRETCH`（注释：合成交换链只支持 STRETCH）、`FLIP_SEQUENTIAL`。

[directx_renderer.rs:L1227-L1256](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1227-L1256)——回退路径：`CreateSwapChainForHwnd` 直接绑定 HWND，`ALPHA_MODE_IGNORE`、`SCALING_NONE`。缓冲数都是 3（[directx_renderer.rs:L1598](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1598)）。

[directx_renderer.rs:L917-L938](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L917-L938)——DComp 三件套 `comp_device/comp_target/comp_visual`；`set_swap_chain` 即 `SetContent(swap_chain) → SetRoot(visual) → Commit` 三步。

管线状态与实例缓冲：

[directx_renderer.rs:L991-L1031](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L991-L1031)——`PipelineState<T>`：一对着色器 + 结构化实例缓冲 + SRV + 混合状态，`PhantomData<T>` 标记实例类型。

[directx_renderer.rs:L1033-L1064](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1033-L1064)——`update_buffer` 的**容量倍增**策略：实例放不下时按 2 的幂扩容，封顶 `MAX_INSTANCE_BUFFER_SIZE = 256MB`（[directx_renderer.rs:L30](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L30)），超限直接报错。缓冲通过 `Map(WRITE_DISCARD)` 整块重写（[directx_renderer.rs:L1540-L1552](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1540-L1552)）。

[directx_renderer.rs:L1112-L1137](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1112-L1137)——`draw_range`：先把批次起始实例号写进 `BatchParams` 常量缓冲（16 字节对齐，[directx_renderer.rs:L982-L989](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L982-L989) 有编译期断言），再 `DrawInstanced(4, instance_count, ...)`——每个 sprite 是 4 顶点三角带（TRIANGLESTRIP），顶点着色器按实例号从结构化缓冲取几何。这就是设备层要求「结构化缓冲支持」的原因。

混合状态与亚像素文本：

[directx_renderer.rs:L1396-L1413](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1396-L1413)——常规混合：标准预乘 alpha 公式 `SRC_ALPHA / INV_SRC_ALPHA`。

[directx_renderer.rs:L1415-L1434](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1415-L1434)——亚像素字形专用：用 `SRC1_COLOR`（dual-source blending，像素着色器输出第二个颜色参与混合），且**不写 alpha 通道**；注释解释：亚像素渲染的文本没有有意义的 alpha，无法参与常规 alpha 混合。

着色器的两种来源：

[directx_renderer.rs:L1600-L1629](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1600-L1629)——`ShaderModule` 枚举列全九个模块（八个渲染 + EmojiRasterization 给 DirectWrite 用）。

[directx_renderer.rs:L1638-L1655](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1638-L1655)——debug 构建用 `D3DCompileFromFile` **运行时编译** `shaders.hlsl` / `color_text_raster.hlsl`（改着色器不用重新构建 rust）；release 构建用 build.rs 预编译的字节数组（[directx_renderer.rs:L1771-L1772](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1771-L1772) 的 `include!(concat!(env!("OUT_DIR"), "/shaders_bytes.rs"))`）。HLSL 源文件就在 crate 的 `src/` 下：`shaders.hlsl`、`color_text_raster.hlsl`、`alpha_correction.hlsl`。

字体参数与 GPU 信息：

[directx_renderer.rs:L756-L769](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L756-L769)——`get_font_info`：用 `OnceLock` 缓存一份 DirectWrite 的渲染参数（伽马、灰度/亚像素增强对比度、BGR 像素排列），放进每帧的 `GlobalParams` 常量缓冲（[directx_renderer.rs:L971-L980](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L971-L980)）。这体现了 DirectWrite 与渲染器的又一处交汇：**文本引擎的显示参数直接成为渲染管线的 uniform**。

[directx_renderer.rs:L726-L754](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L726-L754)——`gpu_specs`：从适配器描述取名字与软件模拟标志，按 VendorId 分派三家驱动版本查询（NVIDIA 走 nvapi、AMD 走 AGS、其余走 DXGI），供「关于」页面展示。

resize 与设备丢失：

[directx_renderer.rs:L394-L436](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L394-L436)——`resize`：先解绑并丢弃旧渲染目标，再 `ResizeBuffers` 交换链，重建资源。注释解释了一个真实场景：**窗口拖到另一块显卡驱动的显示器上时 `ResizeBuffers` 可能返回 device removed 错误**——此处只把错误传出去，交给 4.1 的恢复链路处理。

[directx_renderer.rs:L256-L328](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L256-L328)——`handle_device_lost_impl`：清空 resources/devices（debug 下前后各打一次 `ReportLiveDeviceObjects` 帮助排查泄漏），全套重建，`atlas.handle_device_lost` 重置图集，最后 `skip_draws = true` 等待强制渲染唤醒（`mark_drawable`，[directx_renderer.rs:L771-L773](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L771-L773)，由 [events.rs:L1308-L1313](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/events.rs#L1308-L1313) 调用）。

#### 4.3.4 代码实践

**实践（源码阅读型）：读着色器，画「一个 quad 从场景到屏幕」的数据流。**

1. **实践目标**：把 Rust 侧的实例结构与 HLSL 侧的输入对上。
2. **操作步骤**：
   - 打开 `gpui_windows/src/shaders.hlsl`，找到 quad 顶点着色器（入口名形如 `quad_vertex`，与 [directx_renderer.rs:L1774-L1789](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L1774-L1789) 的 `as_str()` 拼出的入口一致）；
   - 对照 gpui 主 crate 的 `Quad` 图元结构（在 `gpui/src/scene.rs` 中搜索 `struct Quad`）逐一核对字段；
   - 注意顶点着色器如何用 `SV_InstanceID` 加上 `BatchParams.start_index` 索引结构化缓冲。
3. **需要观察的现象**：Rust 结构体字段顺序/类型与 HLSL 结构体声明一一对应（都是 `#[repr(C)]` 与 HLSL 默认打包规则对齐）。
4. **预期结果**：一条「`scene.quads` → `update_buffer`（结构化缓冲）→ `DrawInstanced` → 顶点着色器取实例 → 像素着色器着色」的完整数据流笔记。
5. HLSL 语义细节如与你的显卡驱动文档冲突，以实际编译结果为准；**待本地验证**（可在 Windows + debug 构建下改一行 `shaders.hlsl` 观察运行时重编译，无需重编 Rust）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mono/subpixel 精灵管线的初始缓冲容量是 512，而 polychrome 只有 16？

**答案**：容量按「一帧里该类图元的典型数量」设定——一屏文本动辄几百个字形（mono/subpixel 各算一种），而彩色字形/图片精灵（poly）一帧通常只有个位数到十几个。初始值只是避免频繁扩容的启发式，`update_buffer` 的倍增策略会按需增长。

**练习 2**：`draw` 里 `Surfaces` 批次是空实现（`Ok(())`），这意味着什么？

**答案**：`PaintSurface`（视频帧、无障碍/嵌入表面之类的自定义表面图元，见 [directx_renderer.rs:L719-L724](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_renderer.rs#L719-L724)）在当前 Windows 渲染器上**不会被画出**，调用方不能依赖它在 Windows 上生效；这是平台能力差异的一个实例，与 u2-l1 讲的「默认实现三种姿态」中的能力缺失类似，但发生在渲染内部而非 Platform trait 层。

**练习 3**：设备丢失恢复后为什么要丢弃紧接着的一帧（`skip_draws`）？

**答案**：恢复发生在帧的中途是可能的——旧设备的全部 GPU 纹理（包括图集里的字形）已随设备销毁，而 GPUI 视图缓存里可能还引用着旧图集 tile（[window.rs:L66-L73](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/window.rs#L66-L73) 的注释解释了为何还要配合 `force_render_pending` 绕过视图缓存）。丢弃首帧 + 强制全量重绘，才能保证引用的纹理全部是新建图集里的。

### 4.4 directx_atlas：图集层——字形与图片的合租房

#### 4.4.1 概念说明

图集（atlas）解决的问题是：一屏文本有几百个字形，如果每个字形一张纹理，绘制时要切换几百次纹理绑定，性能崩溃。办法是把小图**装箱进少量大纹理**，绘制同页的精灵可以连续画完。`DirectXAtlas` 实现 gpui 的 `PlatformAtlas` 契约（[platform.rs:L1324-L1336](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L1324-L1336)，仅两个方法：`get_or_insert_with` 与 `remove`），内部用 etagere 库做装箱，按三种纹理类别分房：

| 类别（`AtlasTextureKind`） | 像素格式 | 每像素字节 | 用途 |
| --- | --- | --- | --- |
| Monochrome | `R8_UNORM` | 1 | 普通灰度字形、图标 |
| Subpixel | `R8G8B8A8_UNORM` | 4 | 亚像素抗锯齿字形（RGB 三通道携带横向覆盖） |
| Polychrome | `B8G8R8A8_UNORM` | 4 | 彩色字形（emoji）、图片 |

每个渲染器持有一个 `Arc<DirectXAtlas>` 并通过 `sprite_atlas()` 以 `Arc<dyn PlatformAtlas>` 暴露给 gpui——**文本系统光栅化出字形位图后，正是经这个契约把像素塞进图集**（u8-l2 会从调用方视角再走一遍）。

#### 4.4.2 核心流程

```text
get_or_insert_with(key, build)                # gpui 传入字形/图片的 key
├─ tiles_by_key 命中 → 直接返回 AtlasTile
└─ 未命中
    ├─ build() → (size, bytes)                # 调用方现场光栅化
    ├─ allocate(size, kind)
    │   ├─ 从同类纹理列表「从后往前」找能塞下的
    │   └─ 都塞不下 → push_texture（新建 1024×1024 起，上限 16384×16384）
    ├─ texture.upload(bounds, bytes)          # UpdateSubresource 上传到 GPU
    └─ tiles_by_key.insert(key, tile)

remove(key)                                   # 引用消失
├─ allocator.deallocate(tile)                 # etagere 归还空间
├─ live_atlas_keys -= 1
└─ 计数归零 → 纹理槽位进 free_list 待复用（纹理对象暂不销毁）
```

#### 4.4.3 源码精读

状态与纹体结构：

[directx_atlas.rs:L17-L35](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L17-L35)——`DirectXAtlas(Mutex<DirectXAtlasState>)`：一把 `parking_lot::Mutex` 罩住全部状态（与 `RefCell` 不同，Mutex 允许将来跨线程使用）；状态里三条 `AtlasTextureList`（gpui 提供的带 free_list 的容器，[platform.rs:L1338-L1342](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L1338-L1342)）加一张 `tiles_by_key` 缓存表。每个 `DirectXAtlasTexture` 持有 etagere 装箱器、D3D11 纹理、SRV 视图与活跃 key 计数。

契约实现：

[directx_atlas.rs:L73-L96](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L73-L96)——`get_or_insert_with`：先查缓存（同一字形反复出现时零成本），miss 才调用 `build` 闭包光栅化并分配上传。返回 `Option` 是因为 `build` 可能返回 None（比如字形尚未准备好）。

[directx_atlas.rs:L98-L126](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L98-L126)——`remove`：归还 etagere 空间、减引用计数；整张纹理无引用时把槽位号压进 free_list——下次 `push_texture` 优先复用空槽而不是追加新纹理。

分配与新纹理：

[directx_atlas.rs:L129-L152](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L129-L152)——`allocate`：在同类纹理里**从最后一张往前**找（`iter_mut().rev()`，最新的纹理最可能有连续空间），都失败才新建。

[directx_atlas.rs:L154-L246](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L154-L246)——`push_texture`：新纹理尺寸取 `min(请求尺寸, 16384)` 再 `max(1024)` 兜底；按类别选像素格式与字节深度（常量见 [directx_atlas.rs:L159-L168](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L159-L168)，16384 是 D3D11 的纹理尺寸上限，注释附了微软文档链接）；`CreateTexture2D` 失败（设备丢失）时安静返回 None 交给恢复链路。

上传与越界防护：

[directx_atlas.rs:L279-L320](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L279-L320)——`upload` 用 `UpdateSubresource` + `D3D11_BOX` 把字节写入纹理子区域。开头有一段重要的防御：`UpdateSubresource` 会按 `row_pitch × height` 读源缓冲，若调用方给的切片偏短，**驱动会越界读**（可能多达数 MB），所以先校验长度、不足则记日志跳过——这是一条用注释记录下来的真实教训，值得抄进你的工程笔记本。

设备丢失与测试：

[directx_atlas.rs:L58-L70](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L58-L70)——`handle_device_lost`：换掉设备句柄、清空三条纹理列表与缓存表；旧纹理随旧设备一起消失，字形会在下次 `get_or_insert_with` 时重新光栅化（配合 4.3 的强制重绘）。

[directx_atlas.rs:L392-L419](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L392-L419)——`test_remove_deallocates_tile_space_for_reuse`：这是本讲唯一的渲染栈单元测试。它用 **WARP 软件渲染器**（`D3D_DRIVER_TYPE_WARP`，见 [directx_atlas.rs:L355-L373](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L355-L373) 的 `create_atlas`）创建设备，不依赖真实 GPU 即可跑 CI：插入小图 + 大图断言同纹理、`remove` 大图、再插同尺寸大图断言复用了同一张纹理——验证 free_list 语义。

#### 4.4.4 代码实践

**实践（运行验证型）：在 Windows 上跑图集单元测试。**

1. **实践目标**：亲眼确认 free_list 复用语义，并理解 WARP 软件设备的意义。
2. **操作步骤**：
   - `cargo test -p gpui_windows test_remove_deallocates_tile_space_for_reuse -- --nocapture`；
   - 阅读 [directx_atlas.rs:L355-L373](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/directx_atlas.rs#L355-L373)，注意 `create_atlas` 在 WARP 设备创建失败时返回 None，测试体开头 `let Some(atlas) = ... else { return; }` 直接静默通过——**在无 GPU 的 CI 容器里这个测试不会失败，只会空跑**。
3. **需要观察的现象**：测试通过；若人为把 `D3D_DRIVER_TYPE_WARP` 换成硬件适配器路径则可能在无显示环境失败。
4. **预期结果**：理解「测试对环境的优雅降级」这一写法。**待本地验证**（需 Windows 环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 etagere 分配要「从最后一张纹理往前找」，而不是从第一张找？

**答案**：图集纹理有明确的新旧梯度：最老的纹理往往是长生命周期字形（常用字符），空间碎片化严重；最新纹理是最近分配的，剩余空间更可能是整块连续区域，装箱成功率更高。从后往前找可以更快命中大块空间，也天然让「同一批新出现的字形」聚在同一张纹理里，绘制批次更少。

**练习 2**：`remove` 之后纹理对象为什么不立即销毁，而要等 `live_atlas_keys == 0` 才进 free_list？

**答案**：一张 1024×1024 纹理里住着很多 tile；移除一个 tile 只是那块区域可复用，纹理上还有其他活跃字形时销毁整张纹理会伤及无辜。`live_atlas_keys` 是整纹理级别的引用计数：归零说明这张纹理完全空了，槽位才值得回收（且槽位回收后纹理对象交给 free_list 复用而非立刻 drop，避免频繁创建/销毁 GPU 资源）。

**练习 3**：`PlatformAtlas` 契约为什么把「光栅化」设计成回调（`build` 闭包）而不是让图集自己光栅化？

**答案**：关注点分离与惰性求值。图集层只懂「装箱与上传」，不知道字形怎么渲染、图片怎么解码；把这些知识留在调用方（文本系统/图像系统），图集通过闭包在**缓存未命中时**才索取像素——命中时闭包根本不会执行，光栅化成本为零。这也让同一契约可以服务 macOS 的 Metal 图集与 Windows 的 D3D 图集（u8-l2 对比）。

### 4.5 direct_manipulation：精密触控板手势

#### 4.5.1 概念说明

Windows 传统滚轮消息（`WM_MOUSEWHEEL`）以「档位」（一格三行）为单位，精度不足以支撑像素级平滑滚动与双指缩放。Windows 8 起系统提供 **Direct Manipulation（DM）**框架：应用注册一个 viewport，把精密触控板（`PT_TOUCHPAD`）的指针「认领」进来，系统就在独立线程里做手势识别与**惯性动画**，应用每个帧周期手动 `Update` 一次，回调里拿到带浮点精度的「内容变换」（缩放 + 平移）。

GPUI 的 `DirectManipulationHandler` 做三件事：

1. 建好 DM 管理器/viewport，配置「平移 + 惯性 + 轨道（rails）+ 缩放」的手势集；
2. 在每帧绘制前手动泵一次 `update()`，并把回调累计的事件 `drain_events()` 转发给 GPUI 的输入回调；
3. 把 DM 的内容变换**分类**成两种 `PlatformInput`：纯平移 → `ScrollWheelEvent`（像素级 delta + 触摸阶段），带缩放 → `PinchEvent`。

#### 4.5.2 核心流程

```text
帧循环（window.rs draw_window，每帧）
└─ direct_manipulation.update()          # 手动驱动 DM 识别 + 触发回调
   └─ drain_events() → 逐个 input 回调     # 回调里的事件已翻译成 PlatformInput

系统侧（DM 独立线程）
└─ OnContentUpdated(content)
   ├─ GetContentTransform → [scale, 0, 0, scale, tx, ty]
   ├─ 位移除以 scale_factor（物理像素 → 逻辑像素）
   ├─ 手势分类：
   │   scale ≠ 1        → Pinch（允许 Scroll 晋升为 Pinch）
   │   scale == 1 且无手势 → Scroll
   └─ 生成事件：
       Scroll → ScrollWheelEvent { delta: (dx, dy) 像素, touch_phase }
       Pinch  → PinchEvent      { delta: scale/last_scale - 1.0 }

手势状态机（DM 回报）
├─ RUNNING（惯性被新手势打断）→ end_gesture()：补发 Ended 事件
└─ READY（完全停止）→ end_gesture() + ZoomToRect 复位变换（供下一手势从零开始）
```

#### 4.5.3 源码精读

处理器与初始化：

[direct_manipulation.rs:L21-L29](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L21-L29)——`DirectManipulationHandler` 的字段里最值得注意的是 `scale_factor: Rc<Cell<f32>>` 与 `pending_events: Rc<RefCell<Vec<PlatformInput>>>`：两者都会被**共享进 COM 事件处理器**（DM 的回调运行在别的线程语义里），所以用 `Rc<Cell>`/`Rc<RefCell>` 做共享可变状态。

[direct_manipulation.rs:L32-L91](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L32-L91)——`new`：CoCreateInstance 管理器 → 取 UpdateManager → CreateViewport → `ActivateConfiguration(TRANSLATION_X | TRANSLATION_Y | INERTIA | RAILS_X | RAILS_Y | SCALING)`（轨道模式让横向滚动条只横向滚，对编辑器很关键）→ 设置 `MANUALUPDATE | DISABLEPIXELSNAPPING`（手动逐帧泵 + 不吸附像素，保精度）→ viewport 矩形设成固定的 1000×1000（注释说明：只用于手势识别，不用于视觉输出）→ `Activate` + `Enable` → 注册事件处理器拿到 cookie。调用点在 [window.rs:L155-L156](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/window.rs#L155-L156)，与渲染器同批创建。

指针认领：

[direct_manipulation.rs:L97-L106](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L97-L106)——`on_pointer_hit_test`：只有 `GetPointerType == PT_TOUCHPAD` 的指针才 `SetContact` 认领；普通鼠标/触摸不经过 DM。入口在 [events.rs:L1273-L1276](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/events.rs#L1273-L1276) 的 `handle_dm_pointer_hit_test`（由指针命中测试消息触发）。

事件泵：

[window.rs:L1294-L1306](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/window.rs#L1294-L1306)——`draw_window` 里 `request_frame` 回调之后、真正绘制之前：`direct_manipulation.update()` 泵一轮识别，`drain_events()` 取走积压事件逐个喂给输入回调。**手势事件与帧同步交付**，天然与渲染节流一致。

手势分类与翻译（本模块的核心逻辑）：

[direct_manipulation.rs:L261-L353](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L261-L353)——`OnContentUpdated`：变换数组是 `[scale, 0, 0, scale, tx, ty]`（注释直接给出布局）；`tx/ty` 除以窗口缩放因子换算逻辑像素；三浮点都和上次相等则早退（防抖动，比较用带相对精度的 `float_equals`，[direct_manipulation.rs:L356-L359](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L356-L359)）；分类规则在注释里写得很清楚——**DM 每次更新同时报平移和缩放（捏合时平移也会漂移），所以必须二选一地归类；允许 Scroll→Pinch（捏合常以小幅度平移开场），不允许 Pinch→Scroll**。滚动事件的 delta 是相邻两次的位移差，捏合的 delta 是 \( \frac{\text{scale}}{\text{last\_scale}} - 1 \) 的相对缩放比。

[direct_manipulation.rs:L206-L252](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L206-L252)——`OnViewportStatusChanged`：`RUNNING` 接 `INERTIA`（新手势打断惯性）→ 结束旧手势序列；`READY`（彻底停了）→ 结束手势并 `ZoomToRect` 把内容变换复位回单位变换，好让下一次手势从原点开始；注释提醒 `ZoomToRect` 自身会再触发一轮 RUNNING→READY，所以要靠「上次变换非单位才复位」防无限循环。

[direct_manipulation.rs:L166-L193](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L166-L193)——`end_gesture`：按手势种类补发一条 `touch_phase: Ended` 的零增量事件，让上层（GPUI 的滚动惯性系统）知道序列结束。

DPI 联动：

[events.rs:L857-L872](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/events.rs#L857-L872)——`WM_DPICHANGED` 处理里同步调用 `set_scale_factor(new_scale_factor)`，保证跨不同 DPI 显示器拖动窗口后手势位移换算仍然正确（呼应 u2-l3 的逻辑像素模型）。

#### 4.5.4 代码实践

**实践（源码阅读型 + 运行观察型）：制作触控板手势 → GPUI 事件对照表。**

1. **实践目标**：把 DM 的原始回调数据与 GPUI 收到的最终事件对上。
2. **操作步骤**：
   - 源码部分：填一张表——

     | 触控板动作 | DM 回调 | 生成的 `PlatformInput` | 关键字段 |
     | --- | --- | --- | --- |
     | 双指平移 | `OnContentUpdated`（scale==1） | `ScrollWheelEvent` | `delta` 像素差、`touch_phase: Started→Moved` |
     | 双指捏合 | `OnContentUpdated`（scale≠1） | `PinchEvent` | `delta = scale/last - 1` |
     | 抬指后惯性 | 继续触发 `OnContentUpdated` | `ScrollWheelEvent`（继续） | 直至 `READY` |
     | 手势打断/停止 | `OnViewportStatusChanged` | 零增量 `Ended` 事件 | 见 `end_gesture` |

   - 运行部分（需 Windows + 精密触控板）：给 `direct_manipulation.rs` 的 `pending_events.push(...` 三处临时加 `log::trace!`，或直接开 `RUST_LOG=trace` 跑任意示例，滚动、捏合各做一次。
3. **需要观察的现象**：一次滚动序列的事件数远多于「滚轮一格一条」；惯性阶段事件在抬指后仍持续一小段；`Ended` 事件恰好一条。
4. **预期结果**：对照表与日志吻合。运行部分**待本地验证**。
5. 记得改完源码后还原（本实践若在本地仓库进行，加日志属于临时改动，不要提交）。

#### 4.5.5 小练习与答案

**练习 1**：DM 的 viewport 矩形为什么设成固定 1000×1000 而不是窗口真实尺寸？

**答案**：见 [direct_manipulation.rs:L16-L19](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L16-L19) 的注释：这个 viewport 只承担**手势识别**（认领指针、跟踪变换），不承担任何视觉输出；GPUI 自己管理滚动位置与内容，DM 只是「传感器」。固定尺寸避免了窗口 resize 时同步 viewport 的额外工作。

**练习 2**：为什么允许「滚动升级为捏合」却不允许「捏合降级为滚动」？

**答案**：见 [direct_manipulation.rs:L297-L300](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/direct_manipulation.rs#L297-L300) 的注释：捏合手势开始时常伴有一小段平移（手指聚拢前先微移），这是同一意图的前奏，应当无缝过渡；反之捏合中途改判滚动会造成语义跳变（上层已进入缩放模式再突然变成滚动），且平移的 delta 与缩放导致的平移分量混在一起无法拆干净。一旦判定为 Pinch，直到手势结束都只发捏合事件。

**练习 3**：`DirectManipulationHandler` 里的事件为什么要先攒在 `pending_events` 里，而不是在 DM 回调里直接调用 GPUI 的输入回调？

**答案**：DM 回调发生在 `update()` 调用栈内（主线程逐帧手动泵），但回调的借用环境复杂（COM 回调 → Rust 闭包 → 窗口状态）；把事件先存进共享 `RefCell<Vec<...>>`，回调只做「生产」，`draw_window` 里的 `drain_events` 再统一「消费」，生产/消费解耦后回调实现无需持有窗口借用，也保证事件按帧批量、有序地进入 GPUI——与 u3-l4 光标样式「帧末结算」是同一种延迟结算思想。

## 5. 综合实践

**任务：亲手绘制 Windows 渲染栈分层图，并逐层标注契约对接点。**（本讲义规格中指定的代码实践任务）

要求完成一张分层图（手绘、Mermaid 或纯文本均可），覆盖「设备创建 → 渲染器 → 图集 → 窗口呈现」四层，并满足三个验收标准：

1. **每层注明**：关键类型、所在文件、初始化时机（谁在什么时刻构造它）。参考答案骨架：

   ```text
   ┌────────────────────────────────────────────────────────────┐
   │ 应用/窗口层  WindowsWindow (window.rs)                       │
   │   WindowsWindowState.renderer: RefCell<DirectXRenderer>     │
   │   WindowsWindowState.direct_manipulation                    │
   │   初始化：open_window → WindowsWindow::new → state.new       │
   │   契约：PlatformWindow::sprite_atlas / gpu_specs /           │
   │         schedule_frame(默认空实现，不覆写)                    │
   ├────────────────────────────────────────────────────────────┤
   │ 渲染器层  DirectXRenderer (directx_renderer.rs)              │
   │   DirectXRendererDevices / DirectXResources /               │
   │   DirectXRenderPipelines(8 条) / DirectXGlobalElements /    │
   │   DirectComposition?                                        │
   │   初始化：每个窗口构造时；设备丢失时整体重建                   │
   ├────────────────────────────────────────────────────────────┤
   │ 图集层  DirectXAtlas (directx_atlas.rs)                     │
   │   三条 AtlasTextureList + tiles_by_key + etagere            │
   │   初始化：DirectXRenderer::new 内部；随渲染器 Arc 共享        │
   │   契约：PlatformAtlas::get_or_insert_with / remove           │
   ├────────────────────────────────────────────────────────────┤
   │ 设备层  DirectXDevices (directx_devices.rs)                  │
   │   adapter / dxgi_factory / device / device_context          │
   │   初始化：WindowsPlatform::new（全应用一份，窗口克隆引用）      │
   │   恢复：VSyncProvider 线程检测 → handle_gpu_device_lost      │
   ├────────────────────────────────────────────────────────────┤
   │ 帧驱动  VSyncProvider (vsync.rs) + 平台消息窗口 (platform.rs) │
   │   每 vsync：RedrawWindow(RDW_INVALIDATE) 所有窗口 → WM_PAINT │
   └────────────────────────────────────────────────────────────┘
   ```

2. **画出两条横向链路**并在图上标注：
   - **出帧链路**：vsync 线程 → `RedrawWindow` → `WM_PAINT` → `draw_window` → `renderer.draw(scene)` → 八条管线 → `Present(0,0)`；
   - **字形入图集链路**：文本系统（DirectWrite）光栅化 → `PlatformAtlas::get_or_insert_with`（闭包给像素）→ etagere 分配 → `UpdateSubresource` 上传 → 下一帧 subpixel/mono 管线 `draw_range_with_texture` 从图集 SRV 采样。
3. **标注一个故障注入点**：在图上标出 GPU 设备丢失时每一层各自要做什么（设备层重建四元组、渲染器全量重建并 `skip_draws`、图集清空、文本系统 `handle_gpu_lost`、窗口 `force_render_pending`），并注明这条恢复链路由谁触发（vsync 线程）。

完成后自查三个问题：DirectWrite 在你的图里出现了几次（至少三处：文本系统本体、`GlobalParams` 的字体参数来源、GPU emoji 光栅化）？`schedule_frame` 在 Windows 上有没有实现（没有，默认空实现）？`DirectXDevices` 是每窗口一份还是全应用一份（逻辑上一份、COM 引用计数上多份克隆）？

## 6. 本讲小结

- Windows 渲染栈是清晰的四层结构：**DirectXDevices**（全应用一份的设备四元组，逐卡枚举选出支持结构化缓冲的 GPU）→ **DirectXRenderer**（每窗口一份：交换链 + 八条管线 + 全局常量）→ **DirectXAtlas**（`PlatformAtlas` 契约实现，etagere 装箱三类纹理）→ **WindowsWindow**（`PlatformWindow` 契约，`sprite_atlas`/`gpu_specs` 直接转发渲染器）。
- 帧驱动是 Windows 特有姿态：`VSyncProvider` 线程每个垂直同步周期 `DwmFlush` 后对全部窗口 `RedrawWindow(RDW_INVALIDATE)`，`Present(0,0)` 不再等同步；`schedule_frame` 走默认空实现，与 Wayland 的按需唤醒形成对照。
- 设备丢失恢复是一条跨线程链路：vsync 线程检测 `GetDeviceRemovedReason` → 重建设备 → `SendMessageW` 通知平台窗口与各应用窗口 → 渲染器/图集/文本系统各自重建 → 丢弃一帧后强制全量重绘。
- DirectWrite 与渲染栈的耦合比「文本引擎」更深：它构造时吃进 `DirectXDevices`（emoji 光栅化着色器跑在 GPU 上），又通过 `get_font_info` 把伽马/对比度/BGR 参数注入每帧的 `GlobalParams` 常量缓冲。
- 亚像素文本有专属管线与专属混合状态（dual-source blending、不写 alpha），路径图元走「4x MSAA 中间纹理 → Resolve → sprite 拷回」两阶段，这两个细节是 D3D11 落地 GPUI 视觉要求的关键工程决策。
- `DirectManipulationHandler` 用系统手势框架获得像素级触控板数据：指针认领（PT_TOUCHPAD）、逐帧手动泵、内容变换分类（Scroll/Pinch 二选一、只许升不许降）、状态机复位，最终以 `ScrollWheelEvent`/`PinchEvent` 汇入 `PlatformInput`。

## 7. 下一步学习建议

- **下一讲 u6-l3（菜单、系统通知与跳转列表）**继续留在 Windows/macOS 双平台对照，看 `set_app_identity` 与 `destination_list` 这些 Windows 专属集成——本讲 4.1.3 已经见过 `has_package_identity` 的判断伏笔。
- 想横向对比其他平台的渲染组装，读 `gpui_macos/src/metal_renderer.rs`（Metal 版的同等层）——u8-l2 会以 `PlatformAtlas` 契约为线索把两者放在一起讲。
- 想深入文本系统，预习 `gpui_windows/src/direct_write.rs` 的 `PlatformTextSystem` 实现（本讲只触及其与设备层的耦合点），u8-l1 将完整拆解四套字体栈。
- 依赖阅读路线建议：u2-l1（Platform trait 分组）→ 本讲 → u8-l2（PlatformAtlas 与渲染后端总论）。
