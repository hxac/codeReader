# 多 GPU 与辅助设备

## 1. 本讲目标

本讲是「性能、扩展与高级特性」单元的第一篇，回答一个问题：**instant-ngp 怎么用一台机器上的多块 GPU 来加速？**

读完本讲，你应当能够：

- 说清楚 `CudaDevice` 这个内嵌类如何把「一块 GPU」抽象成「一个带自己 CUDA 流、网络副本、渲染缓冲的独立执行单元」。
- 看懂主设备（primary）与辅助设备（auxiliary）之间的协作：模型参数如何从主设备搬到辅设备、渲染结果如何搬回来、双方用 `signal` / `wait_for` 怎样跨设备同步。
- 追踪一次多视图渲染（尤其 VR 双眼）在 `train_and_render` 里是如何被切分到多块 GPU 上并行执行的。
- 解释清楚一个看似奇怪的「限制」：为什么 `set_mode` 只在 NeRF 模式下才默认开启 `m_use_aux_devices`，以及多 GPU 为什么只用于**渲染**而不用于**训练**。

本讲的代码几乎全部集中在 `src/testbed.cu` 与 `include/neural-graphics-primitives/testbed.h`，跨设备同步的底层原语（`StreamAndEvent`、`SyncedMultiStream`）来自依赖库 tiny-cuda-nn，本仓库只负责调用。

## 2. 前置知识

在进入源码前，先用三段通俗的话把多 GPU 的基础讲明白。

**一台「设备」在 CUDA 里是什么。** 一块 GPU 在 CUDA 运行时里有一个整数编号（device id，从 0 开始）。程序在任一时刻都「当前正使用某一块设备」，所有不显式指定设备的 CUDA 调用都落到这块「当前设备」上。可以用 `cudaSetDevice(id)` 切换当前设备。不同设备各自有**独立的显存**，一块设备看不到另一块设备的指针。

**为什么多 GPU 难。** 如果 GPU 之间显存不互通，那么「在 GPU0 上训练得到的网络权重」就不能被 GPU1 直接拿来用——必须显式地「搬过去」。CUDA 提供了两种搬运方式：普通 `cudaMemcpy`（要先在源端读到可主机中转的内存）和 `cudaMemcpyPeerAsync`（在同一种统一虚拟寻址的 GPU 之间直接拷贝，更快）。搬运需要时间，而且不能乱序：GPU1 必须等 GPU0 把权重写完才能读，否则读到的是半新半旧的脏数据。这就引出了**同步**。

**用「事件（event）」在两条流之间排队。** CUDA 的流（stream）是一条按序执行的命令队列。两条流之间默认没有顺序保证。要让「流 B 等流 A」，标准做法是：在流 A 上记录一个事件（`cudaEventRecord`），再让流 B 等待这个事件（`cudaStreamWaitEvent`）。这样 GPU1 的流就会在硬件层面阻塞，直到 GPU0 把事件记录下来，从而安全地读到一致的数据。instant-ngp 的 `signal` / `wait_for` 就是这套机制的封装。

**一个关键直觉：为什么多 GPU 只帮渲染、不帮训练。** 训练是「一个 Adam 优化器按顺序更新一份参数」，天然串行；而渲染（尤其是 VR 双眼）是「左眼一张图、右眼一张图」，两件事互不依赖、天然可并行。把两只眼睛分给两块 GPU，等于把渲染工作量减半，而训练那一份串行开销不变。这背后就是 Amdahl 定律：

\[
S(n)=\frac{1}{(1-p)+p/n}
\]

其中 \(p\) 是可并行部分占比，\(n\) 是设备数。只有 \(p\) 足够大的工作（如双眼渲染）才值得多 GPU；串行的训练即使加再多 GPU 也加速不了。理解这一点，后面那个「只对 NeRF 开多 GPU」的决定就不奇怪了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/neural-graphics-primitives/testbed.h` | 声明 `Testbed::CudaDevice` 内嵌类、`m_devices` 容器、`primary_device()`、`m_use_aux_devices` 开关，以及 `sync_device` / `use_device` / `set_all_devices_dirty` 等跨设备接口。 |
| `src/testbed.cu` | 实现全部多 GPU 逻辑：`CudaDevice` 构造、`device_guard`、`sync_device`、`use_device`、`set_all_devices_dirty`、设备发现、`set_mode` 默认开关、每设备网络副本、`train_and_render` 的多视图渲染分发。 |
| `include/neural-graphics-primitives/thread_pool.h` | 通用 `ThreadPool`，被 `CudaDevice` 用作辅助设备的渲染工作线程（每个辅设备一个线程）。 |
| （依赖）`dependencies/tiny-cuda-nn` | 提供 `StreamAndEvent`（流+事件封装）、`SyncedMultiStream`（一组同步流）、`GPUMemoryArena`（显存临时分配器）等底层原语。本仓库只调用不实现。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 CudaDevice 抽象**、**4.2 设备切换与同步**、**4.3 多设备渲染**。

### 4.1 CudaDevice 抽象

#### 4.1.1 概念说明

`CudaDevice` 是 `Testbed` 的一个**内嵌类**，它是「一块 GPU」在程序里的化身。回想 u2-l1 我们讲过 `Testbed` 是个承载全部状态的「上帝对象」——多 GPU 也不例外，全部设备状态就挂在 `Testbed` 身上。

一块 GPU 要能独立渲染一个视角，至少需要三样东西：

1. **一条自己的 CUDA 流**：让这块 GPU 的渲染命令可以和别的 GPU 并行排队，而不必挤在主设备的流里串行。
2. **网络的一份只读副本**：渲染只读参数、不修改参数，所以每块辅设备都拷一份当前最新的网络权重和密度网格位域（`density_grid_bitfield`）即可。
3. **一块渲染缓冲**：在这块 GPU 上写出像素颜色与深度，之后再搬回主设备合成上屏。

`CudaDevice` 把这三样连同「我是不是主设备」「我是否需要同步（dirty 标志）」打包成一个对象。于是「多 GPU」在代码层面就是 `Testbed` 持有一个 `std::vector<CudaDevice> m_devices`，其中第一个永远是主设备。

#### 4.1.2 核心流程

- **构造期发现设备**：`Testbed` 构造时，先把当前活动 GPU 作为主设备 `emplace_back` 进 `m_devices`；再遍历所有 GPU，把算力达标的其余 GPU 作为辅设备加进来。
- **建网期复制副本**：`reset_network()` 在主设备上建好网络后，会**对每个设备**都构造一份同结构的网络对象（NeRF 是 `NerfNetwork`，其余是 `NetworkWithInputEncoding`）。注意：这些副本只是「同拓扑」，真正的参数此时还是空的，要等渲染前由 `sync_device` 从主设备拷过来。
- **渲染期按需同步**：每帧渲染前，对每个用到的设备调 `sync_device`，把主设备上**更新过的**参数（密度位域、网络参数、隐藏区域遮罩）拷到辅设备；只有该设备被标记为 `dirty` 时才真正拷贝，避免每帧重复搬运。
- **训练永远只在主设备**：`m_trainer`、反向传播、参数更新都绑定主设备；辅设备拿到的永远是「训练好的快照」。

#### 4.1.3 源码精读

`CudaDevice` 类的声明在 testbed.h 的 1097–1201 行。先看它的「身份」与「装备」：

[CudaDevice 类声明](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1097-L1157) 这段定义了 `CudaDevice`：内嵌 `Data` 结构（密度位域、参数缓冲、隐藏区域遮罩）、构造函数、`device_guard`、以及 `id()` / `is_primary()` / `stream()` / `network()` 等访问器。

几个关键成员（testbed.h 1170–1200）：

- `m_id`、`m_is_primary`：身份标识。
- `m_stream`（`unique_ptr<StreamAndEvent>`）：这块 GPU 自己的流，来自 tiny-cuda-nn。
- `m_data`（`unique_ptr<Data>`）：这块 GPU 自己的密度位域与参数副本。
- `m_render_buffer_view`：渲染内核写入的目标视图（见 u6-l1 的 `CudaRenderBufferView`）。
- `m_network` / `m_nerf_network`：这块 GPU 自己的网络副本。
- `m_fused_render_kernel`：这块 GPU 自己的 JIT 融合内核（JIT 是按设备编译的，见 u8-l2）。
- `m_dirty`：是否需要重新从主设备同步。
- `m_render_worker`：辅设备专属的渲染线程池。

`Testbed` 持有设备容器与主设备访问器：

[m_devices 容器与 primary_device()](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1207-L1213) `m_devices` 是全部设备，`primary_device()` 永远返回 `m_devices.front()`；下面紧跟 `m_thread_pool`、`m_render_futures` 和开关 `m_use_aux_devices`。

设备发现发生在 `Testbed` 构造函数里：

[设备发现：主设备 + 辅设备](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4472-L4492) 先把当前活动设备作为主设备加入；再用 `cuda_device_count()` 遍历所有 GPU，跳过主设备，把算力 `>= MIN_GPU_ARCH` 的其余设备作为辅设备加入。注释明确写了「Multi-GPU is only supported in neRF mode for now」。如果检测到辅设备，会逐块打印其编号、名称与算力。

`CudaDevice` 自己的构造函数：

[CudaDevice 构造函数](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5516-L5521) 在 `device_guard()` 保护下创建自己的流 `m_stream`、数据区 `m_data`，以及一个渲染工作线程池 `m_render_worker`。注意第三行：主设备传 `0u`（0 个工作线程），辅设备传 `1u`（1 个工作线程）——这决定了 `enqueue_task` 的派发方式（见 4.2.3）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认每块设备都持有「同拓扑的网络副本」，但参数要靠运行时同步。

**操作步骤**：

1. 打开 `src/testbed.cu` 的 `reset_network()`，定位到 NeRF 分支（约 4281 行）与非 NeRF 分支（约 4362 行）。
2. 观察这两个分支都形如 `for (auto& device : m_devices) { device.set_nerf_network(...); }` / `device.set_network(...)`——**对每个设备都构造一份**网络。
3. 紧接着的 `m_network = primary_device().nerf_network();`（或 `.network()`）说明：`Testbed` 顶层 `m_network` 指针**只指向主设备那份**；训练与优化只动这份。
4. 再打开 `set_nerf_network` / `set_network`（testbed.cu 5533–5541），注意它们只是把传进来的 `shared_ptr` 存下来——辅设备那份网络的参数缓冲此刻是空的。

**需要观察的现象**：每块辅设备都有一个 `NerfNetwork` 对象，但它的参数指针指向自己设备显存里一块「待填充」的缓冲，真正的数值要等到 `sync_device` 调用 `cudaMemcpyPeerAsync` 才填上。

**预期结果**：你能画出这样一张关系图——

```
m_network (主设备)  ──训练更新──>  params(主)
        │
        │ sync_device + cudaMemcpyPeerAsync（dirty 时）
        ▼
device[1].m_nerf_network ──> params(辅1)   ──只读渲染──> 左眼图
device[2].m_nerf_network ──> params(辅2)   ──只读渲染──> 右眼图
```

（具体运行表现待本地多 GPU 环境验证。）

#### 4.1.5 小练习与答案

**练习 1**：`m_devices` 里第一个元素为什么一定是主设备？代码哪里保证的？

**参考答案**：`primary_device()` 直接返回 `m_devices.front()`（testbed.h 1208），而构造函数（testbed.cu 4472）第一个 `emplace_back(active_device, true)` 就把当前活动 GPU 以 `is_primary=true` 加进去，之后才加辅设备。所以「主设备永远是第一个、且唯一 `is_primary` 为真」是一个由构造顺序保证的不变量。

**练习 2**：为什么 `reset_network` 要给每块设备都建一份网络，而不是所有设备共享同一份？

**参考答案**：因为不同 GPU 的显存互相隔离，GPU1 看不到 GPU0 显存里的指针。共享同一份网络对象意味着所有设备都要去访问主设备显存里的参数，跨设备访问要么不支持、要么极慢。所以每块设备都必须在自己显存里有一份同拓扑的副本，再用 `cudaMemcpyPeerAsync` 把参数值同步过去。

---

### 4.2 设备切换与同步

#### 4.2.1 概念说明

有了多块设备、多份副本，下一个问题是：**怎么让它们按正确的顺序读写，不读到脏数据、也不互相空等？** 这就是「设备切换与同步」要解决的事。instant-ngp 用四个原语拼出整套协作：

- **`device_guard()`**：RAII 守卫，把「当前 CUDA 设备」临时切到本设备，作用域结束时切回原设备。所有要在某块 GPU 上执行的 CUDA 调用，都得先套上对应的 `device_guard`。
- **`signal(stream)` / `wait_for(stream)`**：基于事件的跨流（跨设备）同步。`A.wait_for(b)` 意思是「A 这条流要等 b 这条流把活干完」；`A.signal(b)` 意思是「A 干完后通知 b」。
- **`sync_device`**：渲染前的「参数下行」——把主设备更新过的数据搬到辅设备。
- **`use_device`**：渲染时的「上下文进出」——切到辅设备、分配临时显存、给它一个渲染目标视图，渲染完再把像素结果搬回主设备的渲染缓冲。

#### 4.2.2 核心流程

一次辅设备渲染的同步时间线大致是：

```text
主设备流(m_stream)            辅设备流(device.stream())
─────────────────             ──────────────────────────
训练一步，更新 params           (等待)
sync_device:
  signal → 辅设备流            wait_for ← 收到事件，开始搬数据
                              cudaMemcpyPeerAsync: params 主→辅
                              signal → 主设备流            (参数已就位)
use_device:
  (主设备) wait_for 辅设备流   准备渲染目标 view
  render_frame_main:
                              真正渲染（render_nerf 等）
                              (渲染完成)
                              signal → 主设备流
  ~use_device (ScopeGuard):
    wait_for 辅设备流
    cudaMemcpyPeerAsync: 像素 辅→主
  合成上屏
```

要点：

- 主设备的参数是「源」，辅设备是「汇」，下行用 `cudaMemcpyPeerAsync(辅, 主)`。
- 辅设备渲染出的像素是「源」，主设备缓冲是「汇」，回传用 `cudaMemcpyPeerAsync(主, 辅)`。
- 每一次「源写完」与「汇开读」之间都插一对 `signal`/`wait_for`，保证因果顺序。
- 主设备自己也有这套调用，但走「短路」分支：不拷贝、只设置指针，开销几乎为零。

#### 4.2.3 源码精读

**`device_guard`——切设备的 RAII 守卫：**

[device_guard() 实现](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5523-L5531) 先读当前设备 id；若已经是本设备就返回空守卫（无开销）；否则 `set_cuda_device(m_id)` 切过来，并返回一个 `ScopeGuard`，析构时切回 `prev_device`。这就是为什么前面 `CudaDevice` 构造、`sync_device`、`use_device` 里都要先拿一个 `device_guard`——保证后续 CUDA 调用落到正确的 GPU 上。

**`signal` / `wait_for`——跨流同步契约：**

[wait_for 与 signal](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1127-L1132) `wait_for(stream)`：先在传入的 `stream` 上记录事件 `m_primary_device_event`，再让本设备的流等待该事件——即「本设备流等 `stream` 完成」。`signal(stream)`：本设备流转调 `m_stream->signal(stream)`，即「本设备流完成后通知 `stream`」。`m_primary_device_event` 是个 `cudaEvent_t`（见 Event 结构，testbed.h 1174–1188），作为双方握手的信物。

**`enqueue_task`——主辅设备不同的派发方式：**

[enqueue_task](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1159-L1165) 主设备用 `std::launch::deferred`（延迟到 `get()` 时在**调用线程**就地执行），辅设备把任务投递到自己的 `m_render_worker` 线程池（每辅设备 1 线程）异步执行。这样主设备的渲染任务在主线程同步跑，辅设备的渲染任务在各自线程并发跑，互不阻塞。

**`sync_device`——参数下行（dirty 时才做）：**

[sync_device](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5547-L5598) 开头 `if (!device.dirty()) return;`——只有被标脏的设备才真正同步。主设备走短路：直接把主设备自己的密度位域指针与隐藏遮罩指针塞进 `data()`，清掉 dirty 标志返回（不拷贝）。辅设备才是真正干活：先用 `m_stream.signal(device.stream())` 让辅设备流等主设备流写完；切到辅设备；用 `cudaMemcpyPeerAsync` 把密度位域、网络参数（`m_network->inference_params()`）、隐藏区域遮罩从主设备拷到辅设备，并通过 `device.nerf_network()->set_params(...)` 让辅网络指向新参数；最后 `device.set_dirty(false)` 再 `device.signal(m_stream.get())` 通知主设备「参数已就位」。

**`use_device`——渲染上下文进出（含结果回传）：**

[use_device](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5607-L5658) 先 `device.wait_for(stream)` 确保辅设备等到主设备把数据备好。主设备短路：把渲染缓冲的视图直接交给设备，返回一个清空视图 + `signal` 的守卫。辅设备：切设备、在辅设备流上分配临时显存（frame_buffer 与 depth_buffer）、组装一个 `CudaRenderBufferView` 指向这块临时显存并交给设备；返回的 `ScopeGuard` 在析构时（即渲染已入队之后）把辅设备临时缓冲里的像素与深度用 `cudaMemcpyPeerAsync` 拷回主设备的渲染缓冲，再清空视图并 `signal` 主设备流。一句话：辅设备渲染到一个临时缓冲，作用域结束时把结果「搬运+合并」回主缓冲。

**`set_all_devices_dirty`——标脏全部设备：**

[set_all_devices_dirty](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5660-L5664) 把所有设备 `set_dirty(true)`，强制下一帧 `sync_device` 全量重传。它在网络重建、加载快照、切换隐藏遮罩等参数变化的时机被调用（testbed.cu 4186、4411、4571、5490 等多处），确保辅设备不会用到过期的参数。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：沿「主→辅」与「辅→主」两条数据通路，把 `signal` / `wait_for` 的握手顺序抄出来，验证没有因果倒置。

**操作步骤**：

1. 打开 `sync_device`（testbed.cu 5547–5598）。在辅设备分支里按出现顺序列出同步调用：
   - 第 5559 行 `m_stream.signal(device.stream());`
   - 中间一段 `cudaMemcpyPeerAsync(...)`（参数下行）
   - 第 5597 行 `device.signal(m_stream.get());`
2. 对照 `wait_for` / `signal` 的语义（testbed.h 1127–1132），把上面三步翻译成自然语言：
   - 「主设备流写完后，通知辅设备流可以开始读」
   - 「把参数从主拷到辅」
   - 「辅设备流拷完后，通知主设备流参数已就位」
3. 打开 `use_device`（testbed.cu 5607–5658），同样列出辅设备的握手：
   - 第 5608 行 `device.wait_for(stream);`（辅等主）
   - 渲染（在 `render_frame_main` 里）
   - 析构守卫里第 5655 行 `device.signal(stream);`（辅通知主：结果已生成）
   - 紧接的 `cudaMemcpyPeerAsync`（像素回传）

**需要观察的现象**：每一次「写方」之后都跟着一个让「读方」等待的安排；没有任何一处出现「读方先于写方完成」的依赖。

**预期结果**：你得到一张完整的握手表，验证主辅之间的搬运与渲染严格按 `signal→wait_for` 配对执行。这正是多 GPU 不读脏数据的根本保证（具体运行时序待本地多 GPU 环境验证）。

#### 4.2.5 小练习与答案

**练习 1**：`device_guard()` 在「当前设备已经是本设备」时为什么返回一个空 `ScopeGuard`？

**参考答案**：见 testbed.cu 5524–5527。若 `prev_device == m_id`，说明无需切换，直接返回默认构造的空 `ScopeGuard`（其析构什么都不做）。这是一个零开销快路径，避免在主设备上反复 `set_cuda_device` 造成无谓开销。

**练习 2**：`sync_device` 里第一行 `if (!device.dirty()) return;` 省掉会怎样？

**参考答案**：那样每帧都会对所有辅设备做一次全量 `cudaMemcpyPeerAsync`（密度位域 + 全部网络参数 + 遮罩）。多 GPU 的收益主要来自渲染并行，而这一搬运是串行开销；不判 dirty 会让搬运吃掉相当一部分多 GPU 带来的加速，甚至变成负优化。`dirty` 标志让「参数没变」的帧（绝大多数稳定渲染帧）直接跳过搬运。

---

### 4.3 多设备渲染

#### 4.3.1 概念说明

前两个模块解决了「怎么描述一块设备」和「怎么在设备间安全搬数据」。本模块解决最后一步：**在一帧里，怎么把多个视图（视角）分派到多块设备上并行渲染，再把结果合起来上屏。**

instant-ngp 的渲染是「按视图（view）组织」的。一个视图 = 一个相机 + 一块渲染缓冲。普通单屏渲染只有 1 个视图；VR 双眼渲染有 2 个视图（左眼、右眼）；「可视化多个维度」时会有 N 个视图（u1-l5 提到按维度切屏）。每个视图都关联一个 `device` 指针——这就是多 GPU 的分派开关：

- 单屏/可视化：所有视图都指向 `&primary_device()`，只用主设备。
- VR：若 `m_use_aux_devices` 为真，第 i 个视图指向 `&m_devices.at(i % m_devices.size())`，于是两眼分到两块 GPU。

#### 4.3.2 核心流程

`train_and_render`（GUI 渲染路径）里多设备渲染的分派流程：

```text
1. 组装 m_views（每视图的相机、缓冲、device 指针）
2. 收集本帧用到的去重设备集合 devices_in_use
3. 对每个用到设备：sync_device(其缓冲, 该设备)   # 参数下行
4. 构造 SyncedMultiStream（与 m_views 等数量的同步流）
5. 对每个视图 i：
     futures[i] = view.device->enqueue_task([{
        device_guard = use_device(stream_i, 缓冲_i, 设备_i)  # 切设备+握手+给视图
        render_frame_main(设备_i, 相机_i, ...)               # 真正渲染
     }])
6. 对每个视图 i：
     futures[i].get()                          # 等该视图渲染+结果回传完成
     render_frame_epilogue(stream_i, ...)      # DLSS/色调映射等（主设备流上）
7. 对每个视图：blit_from_cuda_mapping()        # CUDA→GL 上屏
8. cudaStreamSynchronize(m_stream)            # 收尾
```

关键设计：

- **第 5 步用 `enqueue_task` 而非直接调用**：主设备任务在主线程就地执行，辅设备任务在各自工作线程并发执行，于是多块 GPU 真正并行渲染。
- **第 5 步先全部 `enqueue`、第 6 步再逐个 `get`**：这样所有设备的渲染命令能尽快排队，而不是「渲染视图 0 → 等它完 → 再渲染视图 1」地串行。
- **`SyncedMultiStream`**：来自 tiny-cuda-nn，给每个视图一条与主流同步的子流，保证各视图渲染之间不互相干扰，又能与主流正确握手。
- **`render_frame_epilogue` 在主设备流上跑**：色调映射、DLSS 预处理等后处理统一在主设备做（此时像素已回传到主缓冲）。

无头/单缓冲路径（如 `scripts/run.py` 调用的 `render_frame`）更简单：选一个设备（默认主设备），`sync_device` → `use_device` 守卫 → `render_frame_main` → `render_frame_epilogue`，全程单设备。

#### 4.3.3 源码精读

**视图的设备绑定——VR 双眼分 GPU 的开关：**

[VR 视图绑定设备](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L2658-L2658) `m_views[i].device = m_use_aux_devices ? &m_devices.at(i % m_devices.size()) : &primary_device();`。`i % m_devices.size()` 保证视图数多于设备数时循环复用；`m_use_aux_devices` 关时全部退回主设备。注释明说「Render each view on a different GPU (if available)」。

对照单屏渲染的绑定（确认它只用主设备）：

[单屏视图绑定设备](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3253-L3253) `view.device = &primary_device();`——单视图恒用主设备，多 GPU 在此路径不生效。

**多视图渲染分派主循环：**

[多设备渲染分派](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3397-L3458) 先收集去重的 `devices_in_use` 并对每个调 `sync_device`（3397–3406）；再用 `SyncedMultiStream` 给每个视图分配同步流，对每个视图 `enqueue_task` 一个「`use_device` 守卫 + `render_frame_main`」的闭包（3408–3428）；然后逐视图 `futures[i].get()` 等待并跑 `render_frame_epilogue`（3430–3452）；最后 `blit_from_cuda_mapping` 上屏（3455–3457）。

**`render_frame_main` 按模式分发到本设备：**

[render_frame_main](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4904-L4994) 接收一个 `CudaDevice&`，先清它的渲染视图（4915），再按 `m_testbed_mode` 分发：NeRF 调 `render_nerf(device.stream(), device, ..., device.nerf_network(), device.data().density_grid_bitfield_ptr, ...)`（4925–4942）——注意它用的是**本设备**的网络副本与密度位域指针；SDF / Image / Volume 同理都写到 `device.render_buffer_view()`。这就是「每个视图在自己设备上渲染」的落点。

**无头单设备入口 `render_frame`：**

[render_frame（单缓冲入口）](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4870-L4902) `device` 形参默认空时取主设备；依次 `sync_device` →（`use_device` 守卫内）`render_frame_main` → `render_frame_epilogue`。这是 `scripts/run.py` 的 `testbed.render(...)` 最终走到的地方（见 u7-l2），无 GUI、单设备。

**`set_mode` 为何只对 NeRF 默认开多 GPU：**

[set_mode 里的 m_use_aux_devices 默认值](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L229-L235) 只有 `m_testbed_mode == Nerf` 且 `m_devices.size() > 1` 时才 `m_use_aux_devices = true`，其他模式一律 `false`。原因有二：一是只有 NeRF 模式才有 VR 双眼这条「天然两个视图」的渲染路径，多 GPU 才有明确收益（Amdahl 定律里 \(p\) 大）；二是其他三种基元（SDF/图像/体素）没有多视图渲染需求，强开多 GPU 只会增加无谓的参数搬运开销而省不下渲染时间。

**GUI 开关与运行时限制：**

[GUI 多 GPU 复选框](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1347-L1349) 该复选框仅在「设备数 > 1 且当前是 NeRF 模式」时才出现，标签写得很直白：「Multi-GPU rendering (one per eye)」——明确多 GPU 是「每眼一块」的用途。

[VR 目标帧率随多 GPU 提升](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3858-L3860) `init_vr` 里：有多 GPU 时 VR 动态分辨率目标设为 60fps，否则 30fps。这正是多 GPU 的回报——双眼分到两块卡后，单眼渲染预算回到接近单眼水平，于是可以把目标帧率翻倍（见 u6-l1 的动态分辨率机制）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：把一次 VR 双眼渲染的「视图→设备→流→握手」完整调度链手工跑一遍，并解释 `set_mode` 的多 GPU 限制。

**操作步骤**：

1. 假设机器有 2 块达标 GPU。`set_mode(Nerf)` 后 `m_use_aux_devices` 为何？（看 testbed.cu 229–235）→ 因为设备数 2 > 1 且模式是 NeRF。
2. 进入 VR 帧后，`m_views` 有 2 个视图。写出 `m_views[0].device` 与 `m_views[1].device` 分别指向哪块设备（看 testbed.cu 2658，`i % 2`）→ 视图 0→设备 0（主），视图 1→设备 1（辅）。
3. 在分派主循环（testbed.cu 3397–3458）里标注：`devices_in_use` 含哪两块？`sync_device` 分别对它们做了什么？（主：短路设指针；辅：peer 拷贝参数）
4. `enqueue_task` 对两个视图分别走哪条路？（看 testbed.h 1159–1165）→ 视图 0 主设备 `std::launch::deferred`（主线程就地执行），视图 1 辅设备投递到辅设备工作线程。
5. 两个视图的 `render_frame_main` 各自用哪份网络、写入哪个缓冲？（看 testbed.cu 4925–4942 与 `use_device`）→ 各用自己设备的 `nerf_network()`，写入自己设备的临时视图，析构时回传主缓冲。
6. 回答限制题：把 `set_mode` 切到 `Sdf`，`m_use_aux_devices` 变成什么？VR 复选框还会出现吗？（testbed.cu 234 与 1347）→ 变 `false`，复选框消失。

**需要观察的现象**：你能用一张表把「视图 i / 设备 / 派发方式 / 网络副本 / 像素去向」五列填满，且主辅两条链路的 `signal/wait_for` 配对无遗漏。

**预期结果**：得到类似下表（双眼、2 GPU 场景）：

| 视图 | device | enqueue_task 派发 | 使用的网络 | 像素最终去向 |
| --- | --- | --- | --- | --- |
| 0（左眼） | 设备 0（主） | 主线程就地执行 | `primary_device().nerf_network()` | 直接写主缓冲 |
| 1（右眼） | 设备 1（辅） | 辅设备工作线程 | `device[1].nerf_network()`（peer 拷贝） | 临时缓冲→peer 拷回主缓冲 |

（运行时实际并行度与拷贝耗时待本地多 GPU 环境验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么分派循环（testbed.cu 3411–3428）要「先全部 `enqueue_task`，再统一 `get`」，而不是每个视图「enqueue 后立刻 get」？

**参考答案**：`get()` 会阻塞直到该任务完成。若每视图「enqueue 后立刻 get」，则视图 1 要等视图 0 完全渲染完才开始 enqueue，两块 GPU 实际是串行的，多 GPU 形同虚设。先全部 enqueue 让两块 GPU 的渲染命令尽快并发排队，再统一 get 等待，才能真正并行。这是 fork-join 并行的基本写法。

**练习 2**：`render_frame_main` 里 NeRF 分支用的是 `device.nerf_network()` 与 `device.data().density_grid_bitfield_ptr`（testbed.cu 4931–4932），而不是顶层的 `m_nerf_network`。为什么？

**参考答案**：因为渲染发生在「本设备」上，必须读本设备显存里的网络副本与本设备的密度位域指针。顶层 `m_nerf_network` 指向主设备那份，对辅设备而言它的参数指针在主设备显存里、不可直接访问。所以必须用 `device.nerf_network()`，而它的参数已由 `sync_device` 提前 peer 拷贝到本设备。

**练习 3**：Amdahl 定律下，把训练也并行化到多 GPU 能进一步加速 instant-ngp 吗？

**参考答案**：本项目的设计选择是「不并行训练」。训练是单个优化器按顺序更新一份参数，并行化需要跨设备聚合梯度（数据并行），引入的通信与同步开销对 instant-ngp 这种「单 GPU 已秒级训练」的小模型通常得不偿失。多 GPU 的收益集中在「双眼渲染」这种天然两份独立工作的场景，因此代码把它严格限定在 NeRF 渲染路径。

## 5. 综合实践

**任务**：扮演一次「多 GPU 调度器」，把本讲三个模块串起来，画出一次完整 VR 双眼帧（2 块 GPU、NeRF 模式）从「训练一步」到「上屏」的全流程时序图，并标注每一处跨设备交互用到的函数与行号。

要求图中至少包含以下节点与边：

1. `train()` 在主设备上更新 `m_network` 参数（参考 u2-l2、u4-l4）。
2. `set_all_devices_dirty()` 被触发的场景（如网络重建；testbed.cu 5660）。
3. `train_and_render` 组装 2 个 `m_views`，设备绑定（testbed.cu 2658）。
4. 对两个设备的 `sync_device`（testbed.cu 5547），区分主设备短路分支与辅设备 peer 拷贝分支。
5. 两个视图的 `enqueue_task`（testbed.h 1159）分别走主线程就地执行与辅设备工作线程。
6. 每个视图内 `use_device`（testbed.cu 5607）的 `wait_for` → 渲染 → 析构 `signal` + peer 回传。
7. `render_frame_epilogue` 与 `blit_from_cuda_mapping` 上屏（testbed.cu 3437、3456）。

**进阶思考**（可写入你的学习笔记）：

- 如果一台机器有 4 块 GPU，但 VR 只有 2 只眼睛，多出的 2 块会被用到吗？（提示：看 `i % m_devices.size()` 与 `devices_in_use` 去重。）
- `set_all_devices_dirty` 之后的第一帧会比分帧慢，为什么这是可接受的代价？

完成后，你应该能用一句话向别人解释清楚：「instant-ngp 的多 GPU = 把 VR 双眼分给两块卡并行渲染，参数每帧按需从主卡 peer 拷到副卡，结果再 peer 拷回来合成，仅此而已；训练永远只在主卡。」

## 6. 本讲小结

- `CudaDevice` 是「一块 GPU」的化身：自带 CUDA 流（`StreamAndEvent`）、网络副本（`m_network`/`m_nerf_network`）、数据区（`Data`，含密度位域与参数缓冲）、渲染视图与一个辅设备专属工作线程；`Testbed` 用 `std::vector<CudaDevice> m_devices` 持有它们，第一个永远是主设备。
- 设备切换靠 RAII 守卫 `device_guard()`（切 `set_cuda_device` 并自动还原）；跨设备同步靠 `signal` / `wait_for` 这对基于 `cudaEvent_t` 的握手；`sync_device` 负责「参数下行」（dirty 时才 peer 拷贝），`use_device` 负责「渲染上下文进出 + 像素回传」。
- 主设备走各种「短路」分支（`enqueue_task` 用 `deferred`、`sync_device` 只设指针、`use_device` 直接复用主缓冲），辅设备才真正 peer 拷贝与异步派发——所以单 GPU 时多 GPU 代码几乎零额外开销。
- 多视图渲染在 `train_and_render` 里用 fork-join：先对每个用到的设备 `sync_device`，再给每个视图 `enqueue_task` 一个「`use_device` + `render_frame_main`」闭包并发执行，最后统一 `get` 并跑 `render_frame_epilogue` 上屏。
- 视图到设备的绑定（testbed.cu 2658）由 `m_use_aux_devices` 开关控制，VR 下 `i % m_devices.size()` 把多眼分到多卡。
- 多 GPU 严格限定在 **NeRF 渲染**：`set_mode` 只在 NeRF 且设备数 > 1 时默认开启 `m_use_aux_devices`，GUI 复选框也只在此时出现；训练始终单设备串行——这是 Amdahl 定律下的合理取舍，VR 多 GPU 的回报是把目标帧率从 30 提到 60。

## 7. 下一步学习建议

本讲讲清了「多设备并行渲染」的骨架。建议接着读：

- **u8-l2 JIT 融合与全融合内核**：`CudaDevice` 持有自己那份 `m_fused_render_kernel`（testbed.h 1196），因为 JIT 内核是按设备编译的；学完 u8-l2 你会理解为什么每块设备要单独 `set_fused_render_kernel`。
- **u8-l4 DLSS、VR/OpenXR 与注视点渲染**：本讲的 VR 双眼分 GPU 与 u8-l4 的 OpenXR 双眼视图、深度重投影天然衔接；多 GPU 把目标帧率提到 60 正是为了给 DLSS/注视点留预算。
- **u6-l1 渲染缓冲区与 CUDA-GL 互操作**：本讲反复出现的 `CudaRenderBufferView`、`blit_from_cuda_mapping`、`in_resolution`/`out_resolution` 都在 u6-l1 详述，回看能补全「像素回传后如何上屏」的最后一步。
- 想动手验证的读者：在有 2 块同代 NVIDIA GPU 的机器上编译带 GUI 的 instant-ngp，加载一个 NeRF 场景并连接 VR 头显，在 Rendering 面板勾选「Multi-GPU rendering (one per eye)」，观察启动日志里的「Detected auxiliary GPUs」与帧率变化（待本地环境验证）。
