# WgpuContext：instance、adapter、device 与 queue 的创建

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 wgpu 的 **Instance / Adapter / Device / Queue 四层对象模型**，以及每一层分别在什么时机创建、为什么 `WgpuContext` 要同时持有这四样东西。
2. 读懂 [src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs) 中 `create_device` 的「保守申请」策略：为什么 `required_features` 只在适配器已报告 `DUAL_SOURCE_BLENDING` 时才申请它，为什么 `required_limits` 以 `downlevel_defaults()` 为底、再单独放开分辨率与对齐。
3. 理解 `select_color_texture_format` 如何在 `Bgra8Unorm` 与 `Rgba8Unorm` 之间做回退选择，以及这个选择如何一路影响精灵图集的纹理格式与上传数据是否需要交换 R/B 通道。
4. 独立完成一个最小 wgpu 工程：请求适配器、创建 device/queue、打印特性与限制，并与本 crate 的实现逐项对比。

本讲是第二单元「GPU 上下文与初始化」的第一讲。后续三讲（适配器排序、Web 后端、设备丢失）都建立在本讲打下的对象模型之上。

## 2. 前置知识

### 2.1 从零理解：显卡、驱动与 wgpu 的四层对象

如果你只在 CPU 上写过 Rust，可以先这样建立直觉：

- **显卡（GPU）**是一块独立硬件；**驱动**是操作系统里代你管理这块硬件的软件。
- **wgpu** 是一个跨平台的 GPU 抽象层（Zed 使用的版本是 29.0.4，见仓库根 [Cargo.toml:L898](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/Cargo.toml#L898)），它在不同平台上分别对接 Vulkan（Linux/Windows）、Metal（macOS）、DirectX 12（Windows）、OpenGL/WebGL、浏览器 WebGPU。
- wgpu 把「拿到一块能用的 GPU」拆成四步，每步得到一个对象：

| 层级 | 对象 | 通俗类比 | 主要职责 | 创建代价 |
|---|---|---|---|---|
| 1 | `wgpu::Instance` | 装机员 | 加载各后端驱动、枚举适配器、创建表面（surface） | 整个进程一次 |
| 2 | `wgpu::Adapter` | 挑中的一块显卡 | 报告显卡信息、`features()`、`limits()`、格式能力 | 只读查询 |
| 3 | `wgpu::Device` | 在显卡上开的工作账户 | 创建缓冲/纹理/管线等一切资源 | 昂贵 |
| 4 | `wgpu::Queue` | 提交工作的传送带 | `write_buffer`/`write_texture` 与命令提交 | 随 device 一起 |

一个要点：**一个进程通常只需要一个 `Instance` 和少数几个 `Device`**。Zed 是多窗口编辑器，多个窗口共享同一份 GPU 连接——这正是上一讲提到的 `GpuContext`（`Rc<RefCell<Option<WgpuContext>>>`）把 `WgpuContext` 存成可共享句柄的原因。

### 2.2 Features 与 Limits：向驱动「申请权限」

创建 `Device` 时要提交一份 `DeviceDescriptor`，其中两个字段是驱动要「审批」的：

- **`required_features`（特性）**：一组可选能力的开关集合。例如 `DUAL_SOURCE_BLENDING`（双源混合）。如果申请了适配器不支持的特性，**创建 device 直接失败**。
- **`required_limits`（限制）**：一组数值上限。例如 `max_texture_dimension_2d`（2D 纹理最大边长）、`min_uniform_buffer_offset_alignment`（uniform buffer 绑定偏移对齐）。同样，申请超过适配器能力的上限也会失败。

什么是 **downlevel**？WebGPU 规范定义了一套「基线」能力。但世界上存在大量老显卡和 WebGL2 这类弱后端，达不到完整基线。wgpu 为此预备了三档限制模板：

| 模板 | 含义 |
|---|---|
| `Limits::defaults()` | 完整 WebGPU 基线 |
| `Limits::downlevel_defaults()` | 降级基线，几乎所有硬件都该满足 |
| `Limits::downlevel_webgl2_defaults()` | 比 downlevel 更低，专供 WebGL2 |

策略自然是：**按最低档申请，保证 device 创建不因能力不足而失败**——但渲染器确实需要大纹理与正确对齐，所以本 crate 会把这两类限制单独「放开」到适配器实际值，后面 4.3 会精读。

### 2.3 DUAL_SOURCE_BLENDING 与纹理格式速览

- **双源混合**：普通 alpha 混合公式为 \( C_{out} = C_{src} \cdot f_{src} + C_{dst} \cdot f_{dst} \)，其中 \( C_{src} \) 是片元输出、\( C_{dst} \) 是 framebuffer 已有颜色，混合因子 \( f \) 由固定状态给出。双源混合允许片元着色器输出**第二个颜色** \( C_{src1} \)，并让 \( f_{src} \) 直接引用它，从而实现**红绿蓝三个通道各自独立的 alpha**。这是 LCD 亚像素文本抗锯齿（一个像素内 R/G/B 三个子像素分别着色）的关键，细节在 u4-l4 展开。本讲只需知道：它是可选特性，有没有它决定文本渲染质量档位。
- **`Bgra8Unorm` / `Rgba8Unorm`**：都是每像素 4 字节、`Unorm` 表示字节值线性映射到 \([0,1]\)。区别仅在内存中的通道顺序：B-G-R-A 与 R-G-B-A。GPU 原生交换链常见为 BGRA，而 OpenGL 系字节序天然是 RGBA——这解释了后面 `select_color_texture_format` 的平台分叉。

### 2.4 承接上一讲

上一讲（u1-l2）我们确立了本 crate 的渲染主线是 `wgpu_context` → `wgpu_atlas` → `wgpu_renderer`。本讲进入第一环：`WgpuContext` 如何从零建立起与 GPU 的连接。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs)（607 行） | **本讲主战场** | `WgpuContext` 结构体、`new_with_options`、`create_device`、`select_color_texture_format` |
| [src/wgpu_renderer.rs](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs) | 调用方 | `WgpuContext::new` / `new_rejecting_software` / `WgpuContext::instance` 的两处真实调用点 |
| [src/wgpu_atlas.rs](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs) | 消费方 | `color_texture_format` 如何决定图集纹理格式与上传通道交换 |
| [Cargo.toml](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml) | 构建配置 | `wgpu.workspace = true`、wasm 侧 `webgl` feature、原生侧 `pollster` |

说明：本讲刻意**不深入** `select_adapter_and_device` 的四级排序（u2-l2 专讲）、`new_web` 的浏览器路径（u2-l3 专讲）、`device_lost` 回调的线程安全设计（u2-l4 专讲），只把它们作为流程中的一站带过。

## 4. 核心概念与源码讲解

### 4.1 WgpuContext：跨窗口共享的 GPU 连接对象

#### 4.1.1 概念说明

`WgpuContext` 是本 crate 对「一条完整的 GPU 连接」的封装：instance（驱动入口）、adapter（选中的显卡）、device/queue（逻辑设备与命令队列），外加三个**派生能力**字段——它们都是创建 device 时一次性探测出来、之后整个生命周期只读的值：

- `backend`：实际落到哪个后端（浏览器 WebGPU / GL / 原生某后端）。
- `dual_source_blending`：这块卡能否双源混合（决定文本渲染档位）。
- `color_texture_format`：彩色图集纹理该用 BGRA 还是 RGBA。
- `device_lost`：设备丢失标志（`Arc<AtomicBool>`，本讲只关注它存在，机制留到 u2-l4）。

为什么字段要用 `Arc` 包 device/queue？`wgpu::Device`/`wgpu::Queue` 本身就是内部引用计数的轻量句柄，包一层 `Arc` 后，`WgpuContext` 的字段本身就是「共享句柄」，图集等组件直接 `context.device.clone()` 拿走一份（见 [src/wgpu_atlas.rs:L64-L70](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L64-L70)），克隆语义直观统一。

#### 4.1.2 核心流程

`WgpuContext` 的两条创建路径（原生 / Web）与查询接口：

```text
原生路径（本讲主线）                Web 路径（u2-l3 详讲）
─────────────────────              ─────────────────────
new / new_rejecting_software       new_web(canvas, preference)
        │                                  │
        └────────► new_with_options ◄──────┘（new_web_with_backend）
                        │
        ┌───────────────┼──────────────────┐
        ▼               ▼                  ▼
  解析 ZED_DEVICE_ID  select_adapter_    set_device_lost_
  （用户强制指定显卡）  and_device          callback
                        │                  （过滤 Destroy）
                        ▼
                 create_device ◄── 本讲核心
                        │
                        ▼
                 select_color_texture_format ◄── 本讲核心
                        │
                        ▼
                   构造 WgpuContext

查询接口（创建后只读）：
  backend()                    → WgpuBackend
  supports_dual_source_blending() → bool
  color_texture_format()       → wgpu::TextureFormat
  device_lost()                → bool
```

#### 4.1.3 源码精读

先看结构体全貌。[src/wgpu_context.rs:L9-L18](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L9-L18) 定义了 `WgpuContext` 的全部八个字段：`instance`/`adapter` 公有可直接访问，`device`/`queue` 是 `Arc` 共享句柄；`backend`、`dual_source_blending`、`color_texture_format`、`device_lost` 四个字段私有，只能通过访问器读取——这是刻意的封装：**派生能力一旦确定就不该被改写**，渲染器只能「询问」而非「决定」。

[src/wgpu_context.rs:L20-L25](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L20-L25) 定义后端枚举 `WgpuBackend`：浏览器 WebGPU、GL（含原生 OpenGL 与 WebGL2）、以及包裹 `wgpu::Backend` 的 `Native` 变体。为什么不对原生直接用 `wgpu::Backend`？因为浏览器场景需要区分「WebGPU」与「WebGL」这两种 `wgpu::Backend` 里已有但语义重要的取值，统一进一个枚举后，下游一个 `matches!` 就能判断关键分支——见 [src/wgpu_context.rs:L544-L546](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L544-L546) 的 `uses_webgl_instance_data()`：仅当「后端为 GL **且** 编译目标是 wasm」才为真，这个布尔值将开启渲染器一整族 WebGL 特殊分支（实例数据走纹理而非 storage buffer，u3-l6 详讲）。

再看四个访问器。[src/wgpu_context.rs:L548-L554](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L548-L554) 是 `supports_dual_source_blending()` 与 `color_texture_format()`，它们只是把 4.3、4.4 探测结果的只读视图交出去；[src/wgpu_context.rs:L540-L542](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L540-L542) 的 `backend()` 同理。

#### 4.1.4 代码实践

**实践目标**：在不看实现细节的前提下，凭访问器清单推断 `WgpuContext` 在下游被「问」哪些问题。

**操作步骤**（源码阅读型，无需 GPU）：

1. 在仓库根执行 `grep -rn "supports_dual_source_blending\|uses_webgl_instance_data\|\.color_texture_format()" crates/gpui_wgpu/src/`。
2. 对每个命中位置，记录：调用者是谁（renderer 还是 atlas）、拿这个布尔/格式去决定什么。
3. 可选：执行 `cargo doc -p gpui_wgpu --no-deps --open`，在浏览器里浏览 `WgpuContext` 的公开 API 面。

**需要观察的现象**：`color_texture_format()` 的调用点集中在 `wgpu_atlas.rs`，而 `supports_dual_source_blending()` 的调用点集中在 `wgpu_renderer.rs`——探测与消费是分离的。

**预期结果**：你会得到一张「探测方 → 消费方」小表，例如 atlas 的 `from_context`（[src/wgpu_atlas.rs:L64-L70](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L64-L70)）消费 `color_texture_format()`，设备恢复时 `handle_device_lost`（[src/wgpu_atlas.rs:L96-L104](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L96-L104)）会连同 device/queue 一起**重新读取**该格式（因为新设备可能给出不同答案）。`cargo doc` 的效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`WgpuContext` 为什么不同时保存创建 `Device` 时用的那份 `DeviceDescriptor`？

**参考答案**：descriptor 里的 `required_features`/`required_limits` 只在创建瞬间有意义；创建成功后，device 实际生效的能力应通过 `device.features()`/`device.limits()` 查询。真正需要在整个生命周期反复使用的派生结论（能否双源混合、图集格式）已被提炼成两个专门字段，保存原始 descriptor 反而是冗余且易误导的。

**练习 2**：`WgpuBackend` 为什么要新造一个枚举而不是直接用 `wgpu::Backend`？

**参考答案**：因为同一个 `wgpu::Backend::Gl` 在原生（桌面 OpenGL）与 wasm（WebGL2）下要走完全不同的实例数据传输路径，`Native(wgpu::Backend)` 变体把「原生 + 具体后端」与「浏览器 WebGPU」「GL」统一到一个枚举，配合 `cfg!` 编译目标判断，让 `uses_webgl_instance_data()` 一行就能表达「wasm + GL」这个组合条件。

**练习 3**：`device`/`queue` 字段类型是 `Arc<wgpu::Device>`。图集在 `from_context` 里 `context.device.clone()` 克隆的是什么？

**参考答案**：克隆的是 `Arc` 指针（一次引用计数递增），`wgpu::Device` 本身不动。多个持有者（context、图集、渲染器资源）共享同一个逻辑设备，任何一个 drop 都不影响其他人，直到最后一个 `Arc` 释放。

---

### 4.2 new_with_options：原生路径的总入口

#### 4.2.1 概念说明

原生（非 wasm）平台上，`WgpuContext` 的创建全部收敛到私有函数 `new_with_options`。它对外有两个包装，唯一差异是一个 `reject_software: bool` 开关：

- `new`：常规创建，允许软件渲染器（`DeviceType::Cpu`，如 lavapipe）作为兜底。
- `new_rejecting_software`：拒绝软件渲染器。用在**设备丢失恢复**场景——真显卡刚崩过，若回退到软件渲染会让整个 UI 慢到不可用，宁可失败重试（恢复逻辑在 u6-l1 详讲）。

`new_with_options` 本身做四件事：解析环境变量 `ZED_DEVICE_ID` → 委托 `select_adapter_and_device` 完成选择与创建 → 注册设备丢失回调 → 打日志并构造 `Self`。

#### 4.2.2 核心流程

```text
new_with_options(instance, surface, compositor_gpu, reject_software)
│
├─ 1. 读环境变量 ZED_DEVICE_ID
│      ├─ 未设置            → filter = None（正常路径）
│      ├─ 合法 0xABCD/ABCD → filter = Some(u32)（用户强制指定显卡）
│      └─ 非法值/读取失败   → log_err 记录后 filter = None（降级，不崩溃）
│
├─ 2. gpui::block_on(select_adapter_and_device(...))
│      枚举全部适配器 → 四级排序 → 逐个「真配一次表面」验证
│      → 返回 (adapter, device, queue, dual_source_blending, color_texture_format)
│      （排序细节是 u2-l2 的主题）
│
├─ 3. device.set_device_lost_callback(...)
│      reason == Destroy → 忽略（自己主动销毁不算丢失）
│      其他 reason      → device_lost.store(true)（驱动崩溃/休眠恢复等）
│      （标志的共享设计是 u2-l4 的主题）
│
└─ 4. log::info! 选中的适配器名与后端；backend = WgpuBackend::Native(...)
       构造并返回 WgpuContext
```

注意第 2 步用的是 `gpui::block_on`（gpui 提供的同步阻塞执行器）而不是 `async` 函数——原生路径在调用点直接同步等待结果；Web 路径（`new_web`）因为浏览器环境只能异步而设计成 `async fn`。原生侧另一个可用的阻塞执行器是 `pollster`（本 crate 在 [Cargo.toml:L41-L42](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/Cargo.toml#L41-L42) 为非 wasm 目标引入了它）。

#### 4.2.3 源码精读

**两个入口包装**。[src/wgpu_context.rs:L66-L73](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L66-L73) 的 `new` 和 [src/wgpu_context.rs:L75-L82](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L75-L82) 的 `new_rejecting_software` 签名完全一致（instance、surface 引用、可选的合成器 GPU 提示），只是把 `reject_software` 分别置为 `false`/`true` 后转发给 `new_with_options`——一个布尔开关承载两种产品策略。

**ZED_DEVICE_ID 解析**。[src/wgpu_context.rs:L91-L101](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L91-L101) 读取环境变量并用 `parse_pci_id` 解析。注意错误处理的三分支：变量不存在 → `None`；解析失败或读取失败 → `log_err()` 记录后**降级为 None 继续跑**，而不是让 Zed 启动失败——用户敲错一个环境变量不应付出「编辑器打不开」的代价。解析规则在 [src/wgpu_context.rs:L568-L583](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L568-L583)：去空白、剥掉可选的 `0x`/`0X` 前缀、必须恰好 4 个十六进制字符，再 `u32::from_str_radix(.., 16)`。对应的单元测试在 [src/wgpu_context.rs:L589-L605](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L589-L605)，覆盖大小写、前缀有无与位数不足的用例。

**委托与回调**。[src/wgpu_context.rs:L103-L112](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L103-L112) 处的注释值得细读：选择适配器时要「用真实设备实际配置一次表面」来验证兼容性，「这是混合 GPU 系统上唯一可靠的判定方式」——这句注释正是 u2-l2 整讲的主题。[src/wgpu_context.rs:L114-L123](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L114-L123) 注册丢失回调：先 `log::error!` 完整原因与消息，再过滤掉 `DeviceLostReason::Destroyed`（自己 drop 设备触发的「丢失」是正常生命周期，不是故障），其余才置位 `Arc<AtomicBool>`。

**收尾**。[src/wgpu_context.rs:L125-L141](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L125-L141) 打印选中的适配器名称与后端（用户报障时的关键日志），把 `adapter.get_info().backend` 包成 `WgpuBackend::Native`，组装全部八个字段返回。

**Instance 从哪来？** `new_with_options` 只接收而不创建 instance。创建发生在渲染器侧的关联函数 [src/wgpu_context.rs:L289-L298](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L289-L298)：`WgpuContext::instance` 用 `VULKAN | GL` 两个后端构造 `InstanceDescriptor`，并把窗口的 display handle 装进去（`wgpu::wgt::WgpuHasDisplayHandle` 是 wgpu 对 raw-window-handle display handle trait 的再导出）。真实调用点有两处：[src/wgpu_renderer.rs:L282-L285](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L282-L285) 在第一个窗口创建 instance（若共享上下文里已有则直接 `ctx.instance.clone()` 复用），[src/wgpu_renderer.rs:L2093-L2098](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2093-L2098) 在设备丢失恢复时新建 instance 并调用 `new_rejecting_software`。

#### 4.2.4 代码实践

**实践目标**：沿着真实调用点走一遍「instance 创建 → context 创建」的完整链路，确认两条路径（首窗口 / 恢复）的差异。

**操作步骤**（源码阅读型）：

1. 打开 [src/wgpu_renderer.rs:L282-L285](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L282-L285)，向上读约 30 行，弄清 `window` 是什么类型、display handle 从哪来。
2. 打开 [src/wgpu_renderer.rs:L2093-L2098](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2093-L2098)，注意恢复路径：为何先 `sleep(350ms)` 再新建 instance？
3. 用 `grep -n "new_rejecting_software\|WgpuContext::new(" src/wgpu_renderer.rs` 确认两个入口各自的唯一调用点。

**需要观察的现象**：两条路径传入的 `compositor_gpu` 参数来源不同；恢复路径前面有等待逻辑。

**预期结果**：首窗口路径调用 `WgpuContext::new(...)`（允许软件兜底，保证「总能打开」），恢复路径调用 `new_rejecting_software(...)`（拒绝软件，保证「恢复后仍然快」）。`sleep(350ms)` 附近的注释（[src/wgpu_renderer.rs:L2092-L2093](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2092-L2093)）解释了驱动在挂起/恢复后可能需要更多时间回来。完整恢复语义留待 u6-l1。

#### 4.2.5 小练习与答案

**练习 1**：`ZED_DEVICE_ID` 写成 `0xABC`（只有 3 位）会发生什么？

**参考答案**：`parse_pci_id` 因 `len() == 4` 校验失败返回 `Err`；`new_with_options` 里用 `.context(...).log_err()` 记录错误日志后把 filter 置为 `None`，Zed 照常启动并走正常适配器排序。不会 panic、不会启动失败。

**练习 2**：为什么 `new` 与 `new_rejecting_software` 要拆成两个公开函数，而不是给 `new` 加一个 `reject_software: bool` 参数？

**参考答案**：调用点的语义更重要。`new(...)` 读作「常规创建」，`new_rejecting_software(...)` 读作「恢复场景专用」，布尔参数版本在调用处写成 `new(instance, surface, hint, true)`，读者必须查定义才能理解 `true` 的含义；拆分后调用点自解释，且两个包装各自只有一个调用者，没有膨胀。

**练习 3**：`set_device_lost_callback` 里为什么单独放过 `DeviceLostReason::Destroyed`？

**参考答案**：`Destroyed` 是我们自己主动销毁设备（例如窗口关闭、context 重建时 drop 旧 device）时 wgpu 发出的通知，属于**预期内的生命周期事件**；若不过滤，正常的多窗口关闭/恢复流程会误报「设备丢失」，触发不必要的全量资源重建。真正的异常（驱动崩溃、休眠掉卡等）才会走到置位分支。u2-l4 会展开这个标志如何被各渲染器共享。

---

### 4.3 create_device：required_features 与保守 limits

#### 4.3.1 概念说明

`create_device` 是两条平台路径（原生/Web）**共用**的核心函数：输入一个已选定的 adapter，输出 `(device, queue, dual_source_blending, color_texture_format)`。它回答三个问题：

1. **要什么特性？** 先探测 `adapter.features()` 是否含 `DUAL_SOURCE_BLENDING`，**只在已支持时才把它放进 `required_features`**。这个「先查再要」的写法保证特性申请永远不会导致创建失败；探测结果单独作为布尔返回，用于之后决定是否创建亚像素文本管线（u3-l2/u4-l4）。不支持时打 warn 告知用户「亚像素抗锯齿将被禁用」。
2. **要什么限制？** 以 `downlevel_defaults()`（降级基线）为底——这是几乎所有硬件都满足的保守模板；再用 `.using_resolution(adapter.limits())` 与 `.using_alignment(adapter.limits())` 把**分辨率类**（最大纹理边长等）与**对齐类**限制抬到适配器实际值。抬高的这两类来自 `adapter.limits()` 本身，所以同样不可能申请失败；而渲染器真正依赖的恰是这两类——图集纹理尺寸受 `max_texture_dimension_2d` 约束，uniform/storage 的绑定偏移受对齐值约束。wasm 的 GL 后端额外降档到 `downlevel_webgl2_defaults()`。
3. **其他描述符填什么？** `memory_hints: MemoryHints::MemoryUsage`（提示后端内存分配器偏重内存占用而非吞吐，确切语义建议在本地 `cargo doc -p wgpu --open` 查证——待确认）、`trace: Trace::Off`（不录制 API trace）、`experimental_features: disabled()`（显式关闭实验特性，避免行为随 wgpu 版本漂移）。

一句话概括设计哲学：**能不要求的都不要求，必须要求的只要求适配器已声明的**，把「device 创建失败」这个错误面的面积压到最小。

#### 4.3.2 核心流程

```text
create_device(adapter)
│
├─ 1. dual_source_blending = adapter.features().contains(DUAL_SOURCE_BLENDING)
│      ├─ true  → required_features = {DUAL_SOURCE_BLENDING}
│      └─ false → required_features = {} + warn「亚像素抗锯齿禁用」
│
├─ 2. color_atlas_texture_format = select_color_texture_format(adapter)?   ──► 见 4.4
│
├─ 3. required_limits =
│      ├─ 原生:  downlevel_defaults().using_resolution(adapter.limits())
│      │                                    .using_alignment(adapter.limits())
│      └─ wasm:  后端为 Gl → downlevel_webgl2_defaults() 起底，同样放开两类
│                其他    → 与原生相同
│
├─ 4. (device, queue) = adapter.request_device(DeviceDescriptor {
│        label: "gpui_device",
│        required_features, required_limits,
│        memory_hints: MemoryUsage, trace: Off,
│        experimental_features: disabled(),
│     }).await?
│
└─ 5. 返回 (device, queue, dual_source_blending, color_atlas_texture_format)
```

一个值得体会的推论：由于第 1 步的特性是「适配器已报告才申请」、第 3 步的限制是「降级底座 + 适配器自身值」，这份 descriptor 在正常情况下**不会因能力不足而失败**——`request_device` 若仍失败，多半是驱动级的真故障。选择算法（u2-l2）里「逐个适配器试创建」的重试策略正是建立在这个廉价的失败语义上。

#### 4.3.3 源码精读

**特性探测与条件申请**。[src/wgpu_context.rs:L239-L251](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L239-L251)：先用 `adapter.features().contains(wgpu::Features::DUAL_SOURCE_BLENDING)` 探测，再据此决定 `required_features` 内容；不支持时那行 `log::warn!` 明确告诉用户后果（亚像素文本抗锯齿被禁用）。探测与申请是两步，布尔结果穿透返回给上层。

**限制的三套配置**。[src/wgpu_context.rs:L253-L267](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L253-L267)：wasm 分支（L254-L263）先看 `adapter.get_info().backend == wgpu::Backend::Gl`，是则用 `downlevel_webgl2_defaults()` 起底，否则与原生一致；原生分支（L264-L267）直接 `downlevel_defaults()`。两条分支都以 `.using_resolution(adapter.limits()).using_alignment(adapter.limits())` 收尾——把「分辨率类」与「对齐类」限制替换为适配器真实值。

**提交申请**。[src/wgpu_context.rs:L269-L279](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L269-L279) 调用 `adapter.request_device`：label 取 `"gpui_device"`（出现在 wgpu 报错信息里，便于定位），错误被包装成带上下文的 `anyhow` 错误返回。[src/wgpu_context.rs:L281-L286](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L281-L286) 把 device、queue 与两个派生结论一并返回。

**谁在消费这些限制？** 一个直接例子：图集用 `device.limits().max_texture_dimension_2d` 决定单张图集纹理的上限（[src/wgpu_atlas.rs:L52](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L52)）。因为我们申请限制时放开到了适配器真实值，`device.limits()` 拿到的就是显卡的真实上限，不会人为缩水。

#### 4.3.4 代码实践（本讲主实践·上篇）

**实践目标**：在独立最小工程里复刻 `create_device` 的主干，亲手打印出特性与限制，体会「先探测再申请」。

**操作步骤**：

1. 在 zed 仓库**之外**的任意目录新建 cargo 工程：

```bash
cargo new wgpu-device-probe
cd wgpu-device-probe
```

2. 编辑 `Cargo.toml`（示例代码）：

```toml
[package]
name = "wgpu-device-probe"
version = "0.1.0"
edition = "2021"

[dependencies]
wgpu = "29.0.4"      # 与 zed workspace 保持同版本
anyhow = "1"
pollster = "0.4"     # 原生阻塞执行器，与本 crate 的选型一致
```

3. 编写 `src/main.rs`（示例代码——仿写 `create_device` 的最小版本，不含表面验证）：

```rust
use anyhow::Context as _;

fn main() -> anyhow::Result<()> {
    // 对应 WgpuContext::instance 的简化版：不装 display handle，仅探测
    let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
        backends: wgpu::Backends::VULKAN | wgpu::Backends::GL,
        flags: wgpu::InstanceFlags::default(),
        backend_options: wgpu::BackendOptions::default(),
        memory_budget_thresholds: wgpu::MemoryBudgetThresholds::default(),
        display: None,
    });

    let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
        power_preference: wgpu::PowerPreference::HighPerformance,
        compatible_surface: None, // 本例不创建表面，见下方思考题
        force_fallback_adapter: false,
    }))
    .context("request_adapter failed")?;

    let info = adapter.get_info();
    println!("adapter: {} ({:?}, {:?})", info.name, info.backend, info.device_type);
    println!("features has DUAL_SOURCE_BLENDING: {}",
        adapter.features().contains(wgpu::Features::DUAL_SOURCE_BLENDING));

    // === 仿写 create_device 的核心三步 ===
    let mut required_features = wgpu::Features::empty();
    if adapter.features().contains(wgpu::Features::DUAL_SOURCE_BLENDING) {
        required_features |= wgpu::Features::DUAL_SOURCE_BLENDING;
    }
    let required_limits = wgpu::Limits::downlevel_defaults()
        .using_resolution(adapter.limits())
        .using_alignment(adapter.limits());

    let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("probe_device"),
        required_features,
        required_limits,
        memory_hints: wgpu::MemoryHints::MemoryUsage,
        trace: wgpu::Trace::Off,
        experimental_features: wgpu::ExperimentalFeatures::disabled(),
    }))
    .context("request_device failed")?;

    println!("max_texture_dimension_2d: {}", device.limits().max_texture_dimension_2d);
    println!("queue ok: {}", format!("{:p}", &*queue).len() > 0);
    Ok(())
}
```

4. 运行 `cargo run`（需要本机存在 Vulkan 或 GL 驱动；无 GPU 的 CI 容器可能失败，属环境限制）。

**需要观察的现象**：适配器名称与后端；`DUAL_SOURCE_BLENDING` 是否支持；`max_texture_dimension_2d` 的值（常见 16384 或 8192）；与把 `required_limits` 换成 `wgpu::Limits::defaults()` 后运行结果有无差异。

**预期结果**：在你的机器上打印出真实显卡信息；`downlevel_defaults()` 版本几乎必然创建成功，而 `Limits::defaults()` 在老硬件或软件渲染器上**可能**失败——这正是本 crate 选择保守底座的原因。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `required_features` 无条件设为 `Features::DUAL_SOURCE_BLENDING`（不做探测），在一张不支持该特性的老显卡上会发生什么？

**参考答案**：`request_device` 会因「申请了适配器不支持的特性」而返回错误，device 创建失败。原生路径上这个错误会被 `select_adapter_and_device` 的循环捕获并跳到下一个适配器（可能导致整块独显被放弃）；更糟的是如果所有适配器都不支持，Zed 直接无法启动。条件申请把特性「要不要」变成了「探测结果的回声」，消除了这个失败面。

**练习 2**：`.using_resolution(adapter.limits())` 放开的是哪类限制？为什么敢放开，而 `max_storage_buffers_per_shader_stage` 之类保持 downlevel 值？

**参考答案**：`using_resolution` 复制的是分辨率类限制（`max_texture_dimension_1d/2d/3d`、`max_texture_array_layers` 等纹理尺寸上限）。敢放开是因为来源就是 `adapter.limits()` 本身——申请适配器已报告的值不可能失败。而绑定数量、缓冲大小等其他限制，渲染器按 downlevel 基线设计就够了，多要没有收益，反而可能在极端硬件上引入失败风险；只在真正制约功能的两个维度（纹理分辨率、偏移对齐）上取满。

**练习 3**：`create_device` 为什么是 `async fn` 且被原生路径的 `block_on` 包裹调用，而不是同步函数？

**参考答案**：因为 wgpu 的 `request_device` 是异步的，且 **wasm 平台无法使用阻塞执行**——浏览器环境里 `block_on` 会死锁事件循环。所以 `create_device` 必须是 async 供 `new_web_with_backend` 直接 `.await`（[src/wgpu_context.rs:L205-L206](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L205-L206)），原生路径再由 `new_with_options` 用 `gpui::block_on` 驱动到同步边界（[src/wgpu_context.rs:L105-L112](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L105-L112)）。一个实现服务两种异步模型，这是「库代码尽量 async、边界处再同步化」的常见手法。

---

### 4.4 select_color_texture_format：图集格式的选择

#### 4.4.1 概念说明

`select_color_texture_format` 决定**彩色图集纹理**用 `Bgra8Unorm` 还是 `Rgba8Unorm`。它探测的不是「支持与否」（两种格式几乎所有硬件都支持），而是**特定用法组合**是否可用：`TEXTURE_BINDING | COPY_DST`——图集纹理既要被着色器采样（BINDING），又要作为 `write_texture` 上传的目标（COPY_DST）。选择顺序是：

1. **wasm + GL 后端特例**：优先 `Rgba8Unorm`（GL 字节序天然 RGBA）。
2. **原生**：优先 `Bgra8Unorm`（与原生交换链常见格式一致）。
3. 回退 `Rgba8Unorm`（打 warn）。
4. 都不行 → 报错。

这个结果会通过 `color_texture_format` 字段流向图集：`Subpixel`（亚像素文本掩码）与 `Polychrome`（彩色 emoji/图片）两类图集纹理直接采用它，`Monochrome`（灰度文本掩码）固定用单通道 `R8Unorm`。当落到 `Rgba8Unorm` 时，上传路径还要在 CPU 侧交换 R/B 字节——因为字形光栅化的输出字节序是 BGRA。

#### 4.4.2 核心流程

```text
select_color_texture_format(adapter)
│
├─ required_usages = TEXTURE_BINDING | COPY_DST
├─ 探测 bgra = get_texture_format_features(Bgra8Unorm).allowed_usages
├─ 探测 rgba = get_texture_format_features(Rgba8Unorm).allowed_usages
│
├─ [仅 wasm 且后端为 Gl] rgba 满足 → 返回 Rgba8Unorm（GL 字节序优先）
├─ bgra 满足                    → 返回 Bgra8Unorm
├─ rgba 满足                    → warn + 返回 Rgba8Unorm
└─ 都不满足                     → Err（附完整诊断信息）

下游影响（wgpu_atlas.rs）：
  Monochrome           → R8Unorm（与本选择无关）
  Subpixel | Polychrome→ color_texture_format
  上传时若为 Rgba8Unorm → swizzle_upload_data 交换 R/B
```

#### 4.4.3 源码精读

**探测与回退链**。[src/wgpu_context.rs:L502-L514](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L502-L514) 先定义必需用法集合，然后对两种格式各调一次 `adapter.get_texture_format_features`——这是 wgpu 提供的「按格式查询用法能力」接口，比 features 粒度更细。wasm 分支（L506-L511）在「GL 后端且 RGBA 可用」时**提前返回 RGBA**，跳过原生偏好的 BGRA；随后是 BGRA → RGBA 的常规回退。[src/wgpu_context.rs:L515-L525](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L515-L525) 是 RGBA 回退分支，降级时打 warn 说明「该适配器不支持带这些用法的 Bgra8Unorm 图集纹理，回退 Rgba8Unorm」。[src/wgpu_context.rs:L527-L538](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L527-L538) 是彻底失败的错误构造：把两种格式各自允许的用法都打印出来，给驱动级排障留足信息。

**对图集的影响（一）：纹理格式**。[src/wgpu_atlas.rs:L196-L199](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L196-L199) 创建图集纹理时按内容类型分流：`Monochrome`（灰度字形掩码）用单通道 `R8Unorm` 省内存；`Subpixel | Polychrome` 用 `self.color_texture_format`——即本函数的选择结果。图集通过 `from_context`（[src/wgpu_atlas.rs:L64-L70](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L64-L70)）在构造时读取一次，设备恢复时 `handle_device_lost`（[src/wgpu_atlas.rs:L96-L104](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L96-L104)）重新读取——新设备可能给出不同格式，所以恢复路径连格式一起刷新。

**对图集的影响（二）：上传字节序**。[src/wgpu_atlas.rs:L388-L393](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L388-L393) 的 `swizzle_upload_data` 按格式处理上传字节：`Rgba8Unorm` 时逐像素交换 R 与 B，其他格式原样返回。配套的两个纯 CPU 单元测试锁死了行为语义——[src/wgpu_atlas.rs:L511-L517](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L511-L517) 断言 BGRA 纹理原样透传，[src/wgpu_atlas.rs:L520-L524](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L520-L524) 断言 RGBA 纹理收到交换后的字节。由此可以看出 `select_color_texture_format` 偏好 BGRA 的实际收益：**上游（swash/emoji 解码）产出的就是 BGRA 字节流，落到 Bgra8Unorm 纹理可以零拷贝上传**；只有在被迫用 RGBA 纹理时才付出逐字节交换的 CPU 代价。

#### 4.4.4 代码实践

**实践目标**：验证「格式选择 → 图集消费 → 字节交换」这条影响链的行为契约。

**操作步骤**：

1. 运行两个纯 CPU 的 swizzle 测试（不需要 GPU）：

```bash
cargo test -p gpui_wgpu swizzle_upload_data
```

2. 对照 [src/wgpu_atlas.rs:L520-L524](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_atlas.rs#L520-L524) 的断言手推一遍：输入 `10 20 30 40 | AA BB CC DD`，输出为什么是 `30 20 10 40 | CC BB AA DD`？
3. 把 4.3.4 的最小工程扩展一行：`println!("{:?}", adapter.get_texture_format_features(wgpu::TextureFormat::Bgra8Unorm).allowed_usages);`，看本机显卡对 BGRA 的用法支持。

**需要观察的现象**：测试全部通过；扩展打印里包含 `TEXTURE_BINDING` 与 `COPY_DST` 两个标志位。

**预期结果**：步骤 2 中只有每 4 字节组的第 0、2 字节（R 与 B）互换，G、A 不动；步骤 3 在多数桌面显卡上会看到两个用法都可用（即本机会选中 `Bgra8Unorm`）。测试运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Monochrome` 图集纹理不需要参与这个选择？

**参考答案**：灰度字形掩码只有单通道（每个像素一个 alpha 字节），用 `R8Unorm` 即可，相比 4 字节格式节省 75% 显存；它既不存在 BGRA/RGBA 的通道顺序问题，也几乎被所有硬件支持，没有回退决策要做。

**练习 2**：假设某个奇怪的适配器「Bgra8Unorm 支持 TEXTURE_BINDING 但不支持 COPY_DST」，`select_color_texture_format` 会怎么走？

**参考答案**：`bgra_features.allowed_usages.contains(required_usages)` 对**组合**做包含判断，缺 COPY_DST 即为 false，跳过 BGRA 分支；接着检查 RGBA——若 RGBA 两个用法齐全，则打 warn 后返回 `Rgba8Unorm`，后续上传走 `swizzle_upload_data` 交换通道；若 RGBA 也不行，返回携带两种格式完整用法清单的 `Err`。

**练习 3**：`uses_webgl_instance_data()`（[src/wgpu_context.rs:L544-L546](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L544-L546)）与本节的 wasm-GL 特例都在区分「wasm + GL」，两处判断各自服务于什么？

**参考答案**：本节的特例（[src/wgpu_context.rs:L506-L511](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L506-L511)）影响的是**纹理格式选择**——WebGL 字节序天然 RGBA，优先它可省掉上传时的通道交换；`uses_webgl_instance_data` 影响的是**实例数据传输通道**——WebGL2 没有 storage buffer，渲染器必须改用 `Rgba32Uint` 纹理传实例数据并换用对应的着色器变体（u3-l6/u4-l3 详讲）。同一个「wasm + GL」条件在两个维度各自触发不同的降级路径。

---

## 5. 综合实践

把 4.3.4 的最小工程升级为一份**对照审计报告**，贯穿本讲全部四个最小模块。

**任务**：在独立工程中仿写 `create_device` 与 `select_color_texture_format`，然后逐项填写下表（示例代码见 4.3.4；「你的实现」一栏请按你的初版填写，「crate 实现」一栏按源码填写）：

| 维度 | 你的最小实现 | gpui_wgpu 的 `create_device` | 差异带来的后果 |
|---|---|---|---|
| `required_features` | （初版多半是 `empty()`） | 探测到 DSB 才申请它 | 你的版本永远不启用亚像素文本 |
| `required_limits`（原生） | | `downlevel_defaults()` + 放开 resolution/alignment | |
| `required_limits`（wasm + GL） | | `downlevel_webgl2_defaults()` 起底 | |
| `memory_hints` | | `MemoryHints::MemoryUsage` | |
| 图集格式探测 | （初版多半直接写死 Bgra8Unorm） | 用法组合探测 + 三级回退 | |
| 设备丢失处理 | （初版无） | 回调过滤 Destroy 后置位 AtomicBool | |

**具体步骤**：

1. 跑通 4.3.4 的工程，记录你机器上的适配器信息、DSB 支持情况、`max_texture_dimension_2d`。
2. 在你的工程里补一份 `select_color_texture_format` 的仿写（探测两种格式的 `TEXTURE_BINDING | COPY_DST`），打印本机最终会选中哪种格式。
3. 打开 [src/wgpu_context.rs:L236-L287](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L236-L287) 与 [src/wgpu_context.rs:L502-L539](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L502-L539)，逐格填表。
4. 做一个思想实验并写进报告：你的工程若编译到 wasm（`rustup target add wasm32-unknown-unknown`），`required_limits` 必须改成什么样？为什么 `block_on` 在浏览器里不可用、`create_device` 必须保持 async？（对照 [src/wgpu_context.rs:L254-L263](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L254-L263) 与 4.3.5 练习 3 的答案。）
5. 完成后回答：如果让你把这份 `create_device` 移植到一个「只跑在 M 系列 Mac 上」的内部工具里，哪些保守策略可以简化，哪些仍然应该保留？

**预期结果**：一份填满的对照表 + 一段 wasm 差异分析。核心检验标准：你能不查资料地说出「为什么每一项 crate 实现都和你的直觉版不一样」。所有运行结果待本地验证。

## 6. 本讲小结

- **四层对象模型**：`Instance`（驱动入口，进程级共享）→ `Adapter`（物理显卡，只读探测）→ `Device`（逻辑设备，资源工厂）→ `Queue`（命令通道）；`WgpuContext` 把四者连同三个派生能力（backend、双源混合、图集格式）与丢失标志封装成一个跨窗口共享的连接对象。
- **`new_with_options` 是原生总入口**：解析 `ZED_DEVICE_ID`（坏值只降级不崩溃）→ 委托选择算法 → 注册过滤 `Destroyed` 的丢失回调 → 构造 `Self`；`new` 与 `new_rejecting_software` 用一个布尔开关区分「常规创建」与「恢复场景」。
- **`create_device` 的保守申请哲学**：特性「先探测、已支持才申请」（DSB 探测结果单独返回，决定亚像素文本档位）；限制以 `downlevel_defaults()` 为底、只放开分辨率与对齐两类到适配器真实值，wasm 的 GL 后端再降一档——由此把 device 创建的失败面压到近乎只剩驱动级故障。
- **`select_color_texture_format` 三级回退**：wasm-GL 优先 `Rgba8Unorm` → 原生优先 `Bgra8Unorm` → 警告回退 RGBA → 报错；结果决定图集 `Subpixel/Polychrome` 纹理格式，选 RGBA 时上传路径需 `swizzle_upload_data` 逐像素交换 R/B（BGRA 选择可实现零拷贝上传）。
- 探测与消费分离：context 负责一次性探测，atlas 消费格式（设备恢复时重新读取），renderer 消费 DSB 布尔——派生能力以只读访问器暴露，下游只能询问不能改写。

## 7. 下一步学习建议

本讲刻意绕过了三个深水区，它们恰好是本单元接下来的三讲：

1. **u2-l2 适配器选择算法**：`select_adapter_and_device` 的四级排序键（`ZED_DEVICE_ID` → 合成器 GPU 提示 → 设备类型 → 后端优先级）与「真配一次表面」的实测验证法，理解混合 GPU（NVIDIA + Intel）系统的假阳性问题。
2. **u2-l3 Web 平台初始化**：`new_web_with_backend` 的 WebGPU/WebGL2 双后端探测、`WebDisplaySource` 绕过 wgpu-core display handle 检查的技巧，以及 `uses_webgl_instance_data` 开启的渲染分支。
3. **u2-l4 设备丢失检测**：`Arc<AtomicBool>` 标志如何被多个 `WgpuRenderer` 共享、`check_compatible_with_surface` 如何防住多窗口复用上下文时的表面不兼容。

继续阅读源码时，建议把 [src/wgpu_context.rs:L315-L455](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L315-L455)（`select_adapter_and_device`）作为下一讲的预习材料，带着一个问题读：排序第一的适配器为什么还可能被跳过？
