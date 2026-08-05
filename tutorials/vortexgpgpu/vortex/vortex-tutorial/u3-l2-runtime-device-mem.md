# 设备、缓冲区与内存管理

## 1. 本讲目标

在上一讲（u3-l1）中，我们学会了使用主机运行时的公开 API：`vx_dev_open`、`vx_mem_alloc`、`vx_copy_to_dev`、`vx_start` 等等。但我们只把它们当成「黑盒」——只知道调用、不知道内部发生了什么。

本讲打开这个黑盒，深入 `sw/runtime/common` 下的三个核心文件，回答三个问题：

1. **`vx_device_h` 句柄背后到底是什么对象？** 它持有哪些资源，设备内存是怎么「分配」出来的？
2. **`vx_buffer_h` 句柄背后是什么？** 一个缓冲区在运行时内部如何表示，如何被创建和销毁？
3. **`vx_copy_to_dev` / `vx_copy_from_dev` 的数据真正走了哪条路？** 主机数据是如何到达设备内存的？开启虚拟内存后，地址又发生了什么变化？

学完后，你应当能够画出 `device ↔ buffer ↔ vm` 的对象关系图，并标注一次 `copy_to_dev` 经过的每一个对象。

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的内容，尤其是：

- **公开 API 的两层结构**：`vortex2.h`（异步、最小化规范核心）与 `vortex.h`（同步薄封装）。本讲涉及的 `vx_copy_to_dev` 等同步函数，最终都会委托给 `vortex2.h` 的异步原语。
- **不透明句柄**：`vx_device_h`、`vx_buffer_h` 都是 `void*`，能力 ID（`VX_CAPS_*`）、内存标志（`VX_MEM_*`）都在运行时查询，而非编译期宏。
- **stub 分发**：`libvortex.so` 是一个分发器，按 `$VORTEX_DRIVER` 加载后端（simx/rtlsim/opae/xrt）。

此外，你需要了解几个对初学者可能陌生的概念：

- **CP（Command Processor，命令处理器）**：设备上唯一的控制通路与 DMA 引擎。主机运行时「从不直接触碰设备内存」——它只把命令写进 CP 的环形缓冲区（ring），由 CP 去执行。这一点是理解本讲的关键，我们会在 4.1 反复强调。
- **VA 与 PA**：VA（Virtual Address，虚拟地址）是程序（内核）看到的地址；PA（Physical Address，物理地址）是真实在 DRAM 中的地址。当设备开启了 MMU（内存管理单元）时，内核用 VA 访问，硬件的页表遍历器（Page Table Walker，PTW）把 VA 翻译成 PA。这部分逻辑由 `vm.cpp` 在主机侧镜像实现。
- **RISC-V Sv32 / Sv39**：RISC-V 特权架构定义的两种分页模式。32 位用 Sv32（两级页表），64 位用 Sv39（三级页表）。Vortex 根据 `XLEN` 自动选用（见 u2-l1）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `sw/runtime/common`（运行时的「公共核心」，见 u1-l2 的目录地图）：

| 文件 | 作用 |
|------|------|
| [device.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp) | `vx::Device` 类的实现：设备句柄的内部结构、内存分配器、CP 提交通路、`dev_read`/`dev_write` 路由。 |
| [buffer.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp) | `vx::Buffer` 类的实现：缓冲区对象的创建、引用计数、map/unmap。 |
| [vm.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp) | `vortex::VMManager` 类的实现：主机侧镜像的页表构建，PA→VA 映射。 |
| [vortex2_internal.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h) | 上述类的私有声明：`Device`、`Buffer` 的成员字段，是理解对象结构的「图纸」。 |
| [vm.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h) | `VMManager` 与 `DeviceMemIO` 接口的声明。 |
| [mem_alloc.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/mem_alloc.h) | `vortex::MemoryAllocator`——纯主机侧的地址簿记分配器（共享自 `sw/common`）。 |
| [common.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/common.h) | 运行时常量：`ALLOC_BASE_ADDR`、`GLOBAL_MEM_SIZE`、`CACHE_BLOCK_SIZE` 等。 |
| [legacy_runtime.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp) | 同步 API（`vortex.h`）到异步 API（`vortex2.h`）的薄封装，是调用链的起点。 |

> 注意命名空间区分：`Device`/`Buffer` 在 `namespace vx`，而 `MemoryAllocator`/`VMManager` 在 `namespace vortex`。这正是 u2-l3 讲过的「`sw/common` 是唯一合法的跨层共享通道」——公共核心复用了 `sw/common` 的分配器。

## 4. 核心概念与源码讲解

### 4.1 Device 对象：设备句柄的内部结构与地址分配

#### 4.1.1 概念说明

主机程序调用 `vx_dev_open` 拿到一个 `vx_device_h` 句柄。这个句柄的真实身份，是指向 `vx::Device` 对象的指针（见 [vortex2_internal.h:787](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L787) 的 `to_device`，它只是 `static_cast<Device*>(h)`）。

`Device` 对象是这个运行时的「大脑」，它持有三类资源：

1. **后端抽象 `Platform`**：一个纯虚接口（[vortex2_internal.h:119-138](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L119-L138)），由具体后端（simx/xrt/...）实现。`Device` 通过它做 CP 寄存器读写和 CP 可见主机内存的分配，而不关心后端细节。`CallbacksAdapter`（[vortex2_internal.h:151-183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L151-L183)）把 C ABI 的 `callbacks_t` 表桥接成这个 C++ 接口。
2. **设备内存分配器 `MemoryAllocator`**：**纯主机侧的地址簿记**——它只分配地址数字，不分配任何真实存储。这一点至关重要，下文展开。
3. **可选的虚拟内存管理器 `VMManager`**：仅当设备报告自己有 MMU 时才存在。

这里有一个贯穿全讲的核心认知：

> **「分配设备内存」其实只是主机侧发一个地址数字。** 真正的存储是设备的 DRAM；主机运行时并不在分配时触碰它。数据真正进入 DRAM，是在拷贝（`copy_to_dev`）或内核启动时，由 CP 的 DMA 引擎搬过去的。

#### 4.1.2 核心流程：打开设备

`Device::open` 是构造设备的入口，流程是「拿到后端回调 → 构造 `Device` → 初始化 CP」：

[sw/runtime/common/device.cpp:137-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L137-L158) —— 通过 `dispatcher_get_callbacks` 取回后端回调（u3-l3 会详述），`cb->dev_open` 打开底层设备，包成 `CallbacksAdapter`，构造 `Device`，再调 `cp_init()`。注意第 139 行：**每个后端只暴露一个设备**（`index != 0` 即报错），这也是 `vx_device_count` 永远返回 1 的原因。

`Device` 的构造函数本身只做一件事——搭起地址分配器：

[sw/runtime/common/device.cpp:52-79](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L52-L79) —— 这里有两个关键点。第一，`global_mem_` 这个分配器覆盖 `[ALLOC_BASE_ADDR, GLOBAL_MEM_SIZE)` 区间（第 54-55 行），它就是日后所有普通 `vx_mem_alloc` 的地址来源。第二，注释（第 58 行）一针见血地指出：`global_mem_ is pure host-side address bookkeeping; the CP DMAs to whatever addresses it hands out`（全局内存分配器是纯主机侧地址簿记；CP 把数据 DMA 到它分发的任何地址）。

那 `ALLOC_BASE_ADDR` 和 `GLOBAL_MEM_SIZE` 到底是多少？它们定义在 [common.h:26-36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/common.h#L26-L36)：

- `ALLOC_BASE_ADDR = VX_MEM_USER_BASE_ADDR`：来自 `VX_types.toml`，XLEN=64 时是 `0x10000`（64 KiB），XLEN=32 时是 `0x10000`。
- `GLOBAL_MEM_SIZE`：XLEN=64 时 `0x200000000`（8 GB），XLEN=32 时 `0x100000000`（4 GB）。
- `CACHE_BLOCK_SIZE = 64`、`RAM_PAGE_SIZE = 4096`：分配时的对齐粒度。

所以普通分配器管理的是一个从 `0x10000` 开始、长达数 GB 的地址区间。

#### 4.1.3 源码精读：mem_alloc —— 分配设备地址

主机调 `vx_mem_alloc` 时，真正干活的是 `Device::mem_alloc`：

[sw/runtime/common/device.cpp:691-727](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L691-L727) —— 这段代码是理解「设备内存」的钥匙，逐步拆解：

1. **第 694-701 行：`VX_MEM_HOST` 分支**。如果调用者带了 `VX_MEM_HOST` 标志，分配的是「CP 可见的主机内存」——一块主机 RAM，但 CP 的 `m_axi_host` 总线能 DMA 到它。返回的地址 `cp_addr` 既是句柄也是设备侧地址。这正是命令环（ring）和 DMA 暂存缓冲用的内存。
2. **第 702-703 行：对齐**。把请求大小向上对齐到 `CACHE_BLOCK_SIZE`（64 字节）。
3. **第 708-714 行：物理/分页分发**。如果带 `VX_MEM_PHYS` 标志且有 pinned 段（`pinned_mem_`），就从 pinned 段分配；否则从 `global_mem_` 分配。`alloc.allocate(asize, out_addr)` 返回一个 PA。
4. **第 715-725 行：虚拟内存翻译**。这是 VA/PA 分叉的关键。如果设备有 MMU（`vm_mgr_` 非空）：
   - `VX_MEM_PHYS`：调用 `vm_mgr_->install_identity_map`，让 VA==PA（恒等映射），内核直接用该地址访问。
   - 否则：调用 `vm_mgr_->phy_to_virt_map(asize, out_addr, flags)`，**铸造一个全新的 VA 并安装页表项**，然后**把 `*out_addr` 从 PA 改写成 VA**。

也就是说，**当 VM 开启时，`mem_alloc` 返回给调用者的地址是一个 VA**，而不是 PA。内核拿这个 VA 去访问，CP DMA 的 MMU 会把它翻译回 PA。当 VM 关闭时，返回的就是 PA 本身。

`MemoryAllocator` 本身是一个 buddy-style 的空闲块管理器，用「按大小排序的 S 链表」找合适块、用「按地址排序的 M 链表」合并相邻空闲块。它的 `allocate` 方法在 [mem_alloc.h:100-139](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/mem_alloc.h#L100-L139)：遍历已有页找空闲块，找不到就 `findNextAddress` 开新页。整个过程没有任何设备交互。

对应的 `mem_free` 则要先把 VA 翻译回 PA 才能归还给分配器：

[sw/runtime/common/device.cpp:758-784](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L758-L784) —— 第 769-773 行，若 `vm_mgr_` 存在，先 `page_table_walk(addr)` 把 VA 解析成 PA，再按 PA 归还到对应的分配池。这印证了 `mem_free` 收到的「地址」是 VA。

#### 4.1.4 代码实践：观察分配器是纯簿记

1. **实践目标**：确认 `mem_alloc` 不会真的「写」设备内存，它只分发地址数字。
2. **操作步骤**：
   - 阅读 [mem_alloc.h:100-139](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/mem_alloc.h#L100-L139) 的 `allocate`，注意它唯一的「副作用」是修改链表节点和 `allocated_` 计数，没有任何 `dev_write` / DMA 调用。
   - 再阅读 [device.cpp:691-727](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L691-L727) 的 `mem_alloc`，确认除 VM 翻译外，整条路径没有任何设备数据搬运。
3. **需要观察的现象**：分配 1 MB 内存与分配 1 字节内存，在「分配」阶段对设备的开销几乎相同（都是零设备交互）；真正的数据搬运只在后续 `copy_to_dev` 才发生。
4. **预期结果**：你能用一句话解释「为什么 `vx_mem_alloc` 是廉价的、与分配大小无关」——因为它只是地址簿记。
5. 本结论可纯源码阅读得出，无需运行；若想运行验证，可在 `mem_alloc.h` 的 `allocate` 末尾加一行 `printf("allocated pa=0x%lx size=%lu\n", *addr, size);`，再用 `./ci/blackbox.sh --driver=simx --app=demo` 跑一次，观察分配日志与实际数据传输是分离的（运行验证为可选）。

#### 4.1.5 小练习与答案

**练习 1**：`Device::open` 中第 139 行为什么要求 `index == 0`？这对 `vx_device_count` 的返回值有什么约束？

**参考答案**：每个后端只暴露一个设备（`Device::open` 写死 `index != 0` 即返回 `VX_ERR_INVALID_VALUE`），所以 [device.cpp:982](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L980-L984) 的 `vx_device_count` 恒返回 1。

**练习 2**：开启 VM 后，`mem_alloc` 返回的 `*out_addr` 是 PA 还是 VA？为什么 `mem_free` 需要先做一次 `page_table_walk`？

**参考答案**：是 VA。`phy_to_virt_map` 在 [device.cpp:723](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L719-L725) 把 `*out_addr` 改写成了新铸造的 VA。因此 `mem_free` 收到的地址是 VA，必须先用 `page_table_walk` 翻译回 PA，才能归还给按 PA 管理的 `global_mem_` / `pinned_mem_` 分配器（[device.cpp:769-773](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L769-L773)）。

### 4.2 Buffer 对象：缓冲区的创建与生命周期

#### 4.2.1 概念说明

主机调 `vx_mem_alloc` 拿到的是 `vx_buffer_h` 句柄，它的真实身份是 `vx::Buffer*`（[vortex2_internal.h:788](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L788)）。

理解 `Buffer` 的关键，是认识到**它只是一个「瘦」包装器**：它把「一个设备地址 + 一个大小 + 一组标志」打包成一个可引用计数的对象。它本身不持有数据缓冲，数据始终在设备 DRAM 里（或主机镜像里，见 map/unmap）。`Buffer` 的字段只有四个（[vortex2_internal.h:491-494](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L491-L504)）：

- `device_`：回指所属 `Device`。
- `dev_addr_`：设备地址（VM 开启时是 VA）。
- `size_`：大小。
- `flags_`：内存标志（如 `VX_MEM_READ`、`VX_MEM_PHYS`）。

`Buffer` 和 `Device` 都继承自 `RefCounted<T>`（[vortex2_internal.h:80-100](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L80-L100)），用原子引用计数管理生命周期——`retain()` 加一，`release()` 减一，归零时 `delete` 自己。这和 OpenCL 的 retain/release 语义一致。

#### 4.2.2 核心流程：创建一个 Buffer

`vx_mem_alloc` 的实际调用链很短：

```
vx_mem_alloc (legacy_runtime.cpp)        —— 同步封装
  └─ vx_buffer_create (buffer.cpp 的 C 入口)
       └─ Buffer::create
            ├─ dev->mem_alloc(...)       —— 向 Device 要一个地址（4.1 讲过）
            └─ new Buffer(dev, addr,...) —— 包成对象
```

`vx_mem_alloc` 只是把调用转给 `vx_buffer_create`（[legacy_runtime.cpp:82-85](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L82-L85)）。真正的工作在 `Buffer::create`：

[sw/runtime/common/buffer.cpp:33-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L33-L41) —— 先做参数校验，再调 `dev->mem_alloc(size, flags, &dev_addr)` 拿到设备地址，最后 `new Buffer(dev, dev_addr, size, flags)` 把它包起来。注意：**分配失败时直接返回错误，不会构造半成品 Buffer**。

构造函数建立双向联系：

[sw/runtime/common/buffer.cpp:14-18](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L14-L18) —— `device_->retain()`（Buffer 持有 Device 的一个引用，保证 Device 不会先于 Buffer 死去），`device_->register_buffer(this)`（让 Device 知道自己有哪些活着的 Buffer，便于析构时排序）。

析构则做反向清理：

[sw/runtime/common/buffer.cpp:20-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L20-L31) —— 释放主机镜像（若有 map），尽力调用 `device_->mem_free(dev_addr_)` 归还设备地址，从 Device 注销自己，再 `device_->release()`。这就是为什么主机程序里 `vx_mem_free`（它其实是 `vx_buffer_release`，见 [legacy_runtime.cpp:93-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L93-L95)）减引用计数到 0 时，缓冲区会被自动回收。

#### 4.2.3 源码精读：map/unmap 与主机镜像

有些后端不暴露「真正主机可见」的设备缓冲，于是 `Buffer::map` 用一个堆分配的「主机镜像」来模拟 zero-copy 的外观：

[sw/runtime/common/buffer.cpp:59-80](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L59-L80) —— `map_reserve` 用 `std::malloc(size)` 分配一块主机内存作为镜像（第 71 行），记录映射状态，但不搬数据。注意第 65 行的限制：**同一时刻只允许一个映射**。

数据搬运发生在 `map_commit`（读映射时从设备拉数据填镜像）和 `unmap`（写映射时把镜像推回设备）：

[sw/runtime/common/buffer.cpp:82-90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L82-L90) —— `map_commit` 对 `VX_MEM_READ` 映射调用 `device_->dev_read(...)` 从设备读。`unmap`（[buffer.cpp:110-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L110-L123)）对 `VX_MEM_WRITE` 映射调用 `device_->dev_write(...)` 推回。

注释（第 68-70 行）诚实地指出：这种主机镜像方式正确（不会 use-after-free），但**失去了真硬件上 pinned memory 的零拷贝好处**——因为它总是一次完整的搬运。

> 这里出现了 `device_->dev_read` / `dev_write`，它们正是 4.4 要追的数据通路入口。

#### 4.2.4 代码实践：跟踪 Buffer 的引用计数

1. **实践目标**：理解 `Buffer` 是引用计数对象，`vx_mem_free` 只是减引用。
2. **操作步骤**：
   - 阅读 [legacy_runtime.cpp:93-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L93-L95)，确认 `vx_mem_free` 调的是 `vx_buffer_release`，而不是某个「立即销毁」函数。
   - 阅读 [vortex2_internal.h:80-100](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vortex2_internal.h#L80-L100) 的 `RefCounted`，理解 `release()` 在计数归零时 `delete static_cast<T*>(this)`。
   - 再看 [buffer.cpp:20-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/buffer.cpp#L20-L31) 的析构函数，确认是析构（而非 `vx_mem_free` 本身）触发了 `mem_free` 和注销。
3. **需要观察的现象**：若对一个 buffer 连续 `retain` 两次再 `release` 一次，buffer 不会被销毁，设备地址也不会被回收。
4. **预期结果**：能解释「为什么 `vx_enqueue_write` 内部要 `dst->retain()`」（[queue.cpp:187](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L185-L187)）——因为调用者可能在 enqueue 返回后立即 `vx_mem_free`，而真正搬运在 worker 线程异步发生，必须保活 buffer。
5. 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`Buffer` 对象内部存的是设备地址，这个地址在 VM 开启时是 PA 还是 VA？内核和 CP DMA 分别如何理解它？

**参考答案**：是 VA（因为 `Buffer::create` 调的 `mem_alloc` 在 VM 开启时已把地址改写成 VA）。内核直接用这个 VA 访问；CP DMA 则通过自己的 MMU 把 VA 翻译成 PA 再搬运。

**练习 2**：为什么 `Buffer` 构造时要 `device_->retain()`，而析构时要 `device_->release()`？

**参考答案**：保证「Buffer 活着则 Device 必然活着」——避免 Device 先被销毁后，Buffer 析构时调用 `device_->mem_free` 触发 use-after-free。引用计数在 Buffer 销毁时归还这个引用。

### 4.3 VMManager：设备虚拟内存与地址空间

#### 4.3.1 概念说明

`VMManager`（`vortex::VMManager`）是主机侧的「页表构建者」。当设备有 MMU 时，主机运行时必须在分配设备内存后，**主动建立 VA→PA 的页表映射**，否则内核用 VA 访问时会页错误。

`VMManager` 的设计有两个要点（注释见 [vm.h:53-70](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h#L53-L70)）：

1. **影子页表（shadow page table）**：页表内容在主机内存里维护一份镜像（`shadow_pt_`）。所有 `read_pte`/`write_pte` 都打在影子上（纯主机 memcpy，无设备往返）；只有被改过的 PT 页（`dirty_pt_pages_`）才在 `flush()` 时**每页一次**批量 DMA 到设备。这模仿了主流 GPU 驱动（CUDA/ROCm）的做法，让 FPGA 的 DMA 路径高效——分配 1 MB 只需每约 512 个 PTE 一次 DMA，而非 256 次单 PTE 写入。
2. **运行时发现**：`VMManager` 总是被编译进 `libvortex.so`（[vm.cpp:8-10](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L8-L10)），但只在设备报告有 MMU 时才构造。VM 是运行时设备属性，不是编译期 `#ifdef`。

#### 4.3.2 核心流程：初始化与地址空间布局

VM 的初始化发生在 `Device::cp_init` 里，在 CP 启用之后：

[sw/runtime/common/device.cpp:285-307](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L285-L307) —— 流程是：

1. **第 295-297 行**：从 `global_mem_` 里**预留出页表区** `VX_MEM_PAGE_TABLE_BASE_ADDR`（XLEN=64 时 `0xF0000000`，见 [VX_types.toml:26](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L26)），防止后续 `mem_alloc` 把页表区当普通缓冲分发出去。
2. **第 299-301 行**：构造 `CpMemIO` 与 `VMManager`。`CpMemIO`（[device.cpp:219-230](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L219-L230)）是 `VMManager` 的设备内存端口——它的 `read`/`write` 走 CP DMA，且带 `physical=true`（跳过 CP DMA 的 VA 翻译），因为**页表区本身必须按真实 PA 读写**。
3. **第 302-303 行**：`vm_mgr_->init()` 建页表。
4. **第 304-306 行**：把页表根 SATP 编程进 CP 的 `CP_SATP_LO/HI` 寄存器，让 CP DMA 的 MMU 遍历器能找到页表。

`VMManager::init`（[vm.cpp:75-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L75-L119)）做三件事：建页表分配器、建虚拟地址分配器、**对系统区做恒等映射**。第 110-114 行把 IO 区（`[0, USER_BASE)`）和页表+栈的高区恒等映射（VA==PA），因为这些区是按 PA 寻址的。

至此，设备地址空间形成如下布局（XLEN=64 为例）：

| 区间 | 起始地址 | 用途 | 映射方式 |
|------|----------|------|----------|
| IO / MMIO / COUT 环 | `0x40` | 控制台、退出码 | 恒等映射 |
| 用户分配区起点 | `0x10000` (`USER_BASE`) | 普通 buffer、pinned 段 | pinned 恒等 / 普通 VA≠PA |
| 页表区 | `0xF0000000` | SV39 页表 | 恒等映射（PA 直达） |
| 栈 / 本地内存 | `0x1FFFF0000` | 每 warp 栈 | 恒等映射 |
| 全局内存上界 | `0x200000000` (8 GB) | DRAM 顶 | — |

> 这些地址常量都是 HW↔SW 契约（见 u2-l1、u2-l3）：硬件按这些区解码地址，链接器把代码/栈放进对应区，运行时按这些区寻址。

#### 4.3.3 源码精读：phy_to_virt_map —— 铸造 VA

普通 `mem_alloc`（非 PHYS）在 VM 开启时走 `phy_to_virt_map`。它的任务：给一个 PA 区间，分配一段 VA，安装页表项，把输入地址改写成 VA。

[sw/runtime/common/vm.cpp:181-255](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L181-L255) —— 关键步骤：

1. **第 182-183 行**：`need_trans` 判断是否需要翻译——SATP 未设或 BARE 模式时直接返回（不改写）。
2. **第 189-190 行**：计算页数，注意**向上取整**。注释（第 187-188 行）提醒：一个不足 4 KB 的 buffer 仍需一个 PTE，简单的 `size >> PAGE_LOG2` 会把小于一页的分配截断成 0 页导致漏映射。
3. **第 192-238 行**：分配基础 VA。支持随机化（`VORTEX_RANDOMIZE_VA` 环境变量，第 49-59 行构造时读取），默认顺序分配。
4. **第 240 行**：组合最终 VA = `(base_vpn << 12) | 页内偏移`，偏移来自原 PA 的低位。
5. **第 242-249 行**：逐页 `update_page_table(ppn, vpn, flags)` 安装 PTE，记入 `addr_mapping`。
6. **第 251 行**：**断言往返一致**——`page_table_walk(init_vAddr) == init_pAddr`，确保刚装的映射能正确翻译回去。
7. **第 253-254 行**：把输出地址改写成 VA，`flush()` 落盘脏页表页。

页数计算用一个简单的数学关系。设页大小 \(P = 2^{12} = 4096 \)，则覆盖 `size` 字节需要的页数为：

\[
\text{num\_pages} = \left\lceil \frac{\text{size}}{P} \right\rceil = \frac{\text{size} + P - 1}{P}\ \text{（整数除法）}
\]

这正是第 189 行 `(size + VX_VM_PAGE_SIZE - 1) >> VX_VM_PAGE_LOG2_SIZE` 的写法。

`update_page_table`（[vm.cpp:266-316](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L266-L316)）是 Sv32/Sv39 的多级页表遍历：从根 PPN 出发，按每级 VPN 字段查 PTE；遇到无效 PTE 就在叶子级写入映射、在中间级分配下一级表。第 283-294 行特别处理「已有叶子大页覆盖」的幂等情况——RISC-V 规范规定，R/W/X 任一置位的 PTE 是叶子而非下一级指针。

#### 4.3.4 源码精读：page_table_walk 与 flush

`page_table_walk`（[vm.cpp:354-394](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L354-L394)）是 VA→PA 的软件遍历，镜像了硬件 PTW 的行为：按级取 PTE，无效则抛 `Page_Fault_Exception`，找到叶子后重建 PA。第 385-393 行处理大页（mega/gigapage）的偏移：叶子在 level i>0 时，低位偏移来自 VA 而非叶子 PPN。

`flush`（[vm.cpp:445-455](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.cpp#L445-L455)）把每个脏 PT 页用一次 `dev_io_->write` 推到设备——这是「批量更新」的落点。

#### 4.3.5 代码实践：阅读页表区为何要预留

1. **实践目标**：理解为什么 `cp_init` 要在分配器里预留页表区。
2. **操作步骤**：
   - 阅读 [device.cpp:293-298](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L293-L298)，注意 `global_mem_.reserve(VX_MEM_PAGE_TABLE_BASE_ADDR, VX_VM_PT_SIZE_LIMIT)`。
   - 阅读 `MemoryAllocator::reserve`（[mem_alloc.h:71-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/mem_alloc.h#L71-L98)），注意它会检查重叠（`hasPageOverlap`）。
3. **需要观察的现象**：若不预留，后续一次普通 `mem_alloc` 可能分发出一个落在 `0xF0000000` 页表区内的 PA，导致页表数据被普通 buffer 覆盖。
4. **预期结果**：你能解释「预留 = 在分配器的空闲链表里提前占坑，使后续分配绕开该区」。
5. 本实践为源码阅读型，无需运行。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `VMManager` 要维护影子页表，而不是每次直接写设备页表？

**参考答案**：读 PTE 走主机 memcpy（无设备往返），写 PTE 先打影子、按页批量 `flush` 到设备。这让一次大分配只需每约 512 个 PTE 一次 DMA，而非逐 PTE 往返——这是主流 GPU 驱动的通用模式，对 FPGA 的慢 DMA 路径尤其重要（见 [vm.h:60-66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/vm.h#L60-L66)）。

**练习 2**：`CpMemIO` 的 `read`/`write` 为什么带 `physical=true`？

**参考答案**：`CpMemIO` 是 `VMManager` 用来读写**页表区本身**的端口（[device.cpp:219-230](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L219-L230)）。页表区用 PA 寻址，必须让 CP DMA 跳过 VA 翻译，否则会陷入「用页表翻译页表地址」的自举死循环。`physical=true` 设置的 `CP_MEM_FLAG_PHYSICAL` 头标志正是干这个的。

### 4.4 数据通路：copy_to_dev / copy_from_dev 的完整调用链

#### 4.4.1 概念说明

现在把三个对象串起来。主机调 `vx_copy_to_dev` 时，数据要经历这样一条路：

```
主机内存
  → CP 可见主机暂存（staging）
    → CP 环形缓冲区（ring）里的一条 CMD_MEM_WRITE 命令
      → CP 的 DMA 引擎搬运
        → 设备 DRAM（VA 经 MMU 翻译成 PA）
```

记住 4.1 的核心认知：**主机运行时从不直接碰设备内存**。它只做两件事——把数据 memcpy 进 CP 可见的主机暂存区，再把一条命令写进 CP 环。剩下的是 CP 的事。

#### 4.4.2 核心流程：从同步 API 到 CP 命令

完整调用链如下（`vx_copy_to_dev` 为例）：

```
vx_copy_to_dev (legacy_runtime.cpp:115)        同步入口
  └─ enqueue_and_wait                           取默认队列，提交后等事件
       └─ vx_enqueue_write (vortex2.h)
            └─ Queue::enqueue_write (queue.cpp:179)
                 │  worker 线程异步执行 lambda：
                 └─ device_->cp_submit_mem_write(dst->dev_address()+off, host, sz)
                      └─ Device::cp_submit_mem_write (device.cpp:631)
                           ├─ host_alloc(staging)            分配 CP 可见主机内存
                           ├─ memcpy(staging, host_src)       把数据搬进暂存
                           ├─ cp_submit_mem_(MEM_WRITE, dev_dst, staging.cp_addr, size)
                           │    └─ 构造 28 字节命令描述符 → cp_submit_cl_
                           └─ host_free(staging)              释放暂存
                                └─ cp_submit_cl_ (device.cpp:393)
                                     ├─ memcpy 写一条 CL 进 ring
                                     ├─ 写 CP_Q_TAIL 敲门铃（doorbell）
                                     └─ 轮询 CP_Q_SEQNUM 等完成
```

注意 `dst->dev_address() + off` 这个地址：它是 Buffer 里存的 VA（VM 开启时）。CP DMA 收到这条命令后，用自己 MMU 把 VA 翻成 PA，再去 DRAM 搬运。

#### 4.4.3 源码精读：同步入口与队列派发

同步入口把异步操作包成「提交 + 等待」：

[sw/runtime/common/legacy_runtime.cpp:115-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L115-L124) —— `enqueue_and_wait`（定义在 [legacy_runtime.cpp:30-41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L30-L41)）取设备的默认队列，提交一个 `vx_enqueue_write`，然后阻塞等它的事件到达值 1。这就是 u3-l1 讲过的「每个同步调用 = 一次异步提交 + 一次事件等待」。

队列 worker 真正执行搬运：

[sw/runtime/common/queue.cpp:179-206](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L179-L206) —— `enqueue_write` 先校验越界（第 183 行：`off + sz > dst->size()`），`retain` 保活 buffer（第 187 行），把工作塞进一个 lambda 由 worker 线程异步执行。lambda 里调 `device_->cp_submit_mem_write(dst->dev_address() + off, host, sz)`（第 196 行）。`enqueue_read`（[queue.cpp:208-231](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L208-L231)）结构对称，方向相反。

#### 4.4.4 源码精读：CP 提交与命令描述符

`cp_submit_mem_write` 把数据暂存化，再发命令：

[sw/runtime/common/device.cpp:631-646](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L631-L646) —— 三步：(1) `host_alloc` 分配一块 CP 可见主机内存作暂存；(2) `memcpy` 把调用者的数据搬进暂存；(3) 发 `CMD_MEM_WRITE`，参数是「设备目标地址 + 暂存的 cp_addr + 大小」；(4) 释放暂存。注释点明：`physical` 标志（页表写时为 true）让 CP DMA 跳过 VM 翻译。

命令描述符的拼装：

[sw/runtime/common/device.cpp:608-623](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L608-L623) —— 一条 `CMD_MEM_*` 命令是 28 字节：字节 0-3 是头（操作码 + 标志），4-11 是 dst 地址，12-19 是 src 地址，20-27 是 size。这条命令最终塞进一个 64 字节的缓存行（CL）。

`cp_submit_cl_`（[device.cpp:393-455](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L393-L455)）是所有命令的统一出口：把 CL memcpy 进 ring（第 411 行），加 release 栅栏（第 421 行，确保 CP 不读到半写的 ring 项），写 `CP_Q_TAIL` 敲门铃（第 424-427 行），然后轮询 `CP_Q_SEQNUM` 等这条命令退休（第 434-442 行）。

> 这里有一条值得注意的同步纪律：第 405-428 行，`cp_mu_` 只在「写 ring + 敲门铃」期间持有，**轮询前就释放锁**，这样其他提交者（比如另一个队列发 SIGNAL）能在轮询间隙进来，避免死锁。

#### 4.4.5 代码实践：跟踪一次 copy_to_dev

1. **实践目标**：把 4.4.2 的调用链在源码里逐一对应，亲手走一遍。
2. **操作步骤**：
   - 打开 [legacy_runtime.cpp:115-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L115-L124)，确认入口调 `enqueue_and_wait`。
   - 跳到 [queue.cpp:179-206](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L179-L206)，确认 worker 调 `cp_submit_mem_write`。
   - 跳到 [device.cpp:631-646](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L631-L646)，确认暂存化 + 命令提交。
   - 跳到 [device.cpp:393-455](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L393-L455)，确认 ring 写入 + doorbell + 轮询。
3. **需要观察的现象**：在整条链上，**主机运行时从未直接写设备 DRAM**；它只写「CP 可见主机内存」和「CP 环」。
4. **预期结果**：你能回答「数据从主机到设备 DRAM，真正搬运它的是谁」——是 CP 的 DMA 引擎（`VX_cp_dma`），主机只是把数据和命令摆到 CP 够得着的地方。
5. 可选运行验证：在 `cp_submit_mem_`（[device.cpp:608](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L608-L623)）里临时加一行 `fprintf(stderr, "CMD_MEM op=0x%02x dst=0x%lx size=%lu\n", opcode, arg0, arg2);`，用 `./ci/blackbox.sh --driver=simx --app=demo` 跑一次，观察每条 `CMD_MEM_WRITE` 的目标地址（应为 buffer 的 VA）与大小。运行验证为可选。

#### 4.4.6 小练习与答案

**练习 1**：`cp_submit_mem_write` 为什么要先把数据 memcpy 进一块「CP 可见主机内存」暂存，而不是直接用调用者的 `host_ptr`？

**参考答案**：调用者的 `host_ptr` 是普通主机内存，CP 的 `m_axi_host` 总线不一定能 DMA 到它。`host_alloc` 分配的是 CP 能 DMA 的主机内存（[device.cpp:669-681](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L669-L681)），其 `cp_addr` 是 CP 侧地址。命令里填的是暂存的 `cp_addr`，CP 才搬得动。

**练习 2**：如果设备开启了 VM，`copy_to_dev` 命令里的目标地址是 VA，CP DMA 怎么知道对应的 PA？

**参考答案**：CP 在 `cp_init` 时被编程了 SATP（页表根，[device.cpp:304-306](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L304-L306)）。CP 的 DMA 引擎自带 MMU 遍历器，按 SATP 找到页表，把 VA 翻译成 PA。页表则是主机侧 `VMManager` 构建并 `flush` 到设备的那份。唯一例外是带 `physical` 标志的命令（如页表区写入），跳过翻译。

## 5. 综合实践

把本讲三个对象串成一张关系图。请完成以下任务：

1. **画对象关系图**：画出 `Device`、`Buffer`、`VMManager`、`MemoryAllocator`（`global_mem_`/`pinned_mem_`）、`Platform` 之间的持有与回指关系。要求标注：
   - `Device` 持有 `Platform`（后端抽象）、`global_mem_`/`pinned_mem_`（地址簿记）、可选的 `vm_mgr_`（VMManager）。
   - `Buffer` 回指 `Device`，并存一个 `dev_addr_`（标注：VM 开启时是 VA）。
   - `VMManager` 通过 `CpMemIO`（physical 模式）写页表区。

2. **标注一次 `copy_to_dev` 经过的对象**：在你画的图上，用箭头标出数据流——
   - 调用者 `host_ptr`
   - → `Queue`（默认队列，worker 线程）
   - → `Device::cp_submit_mem_write`：经 `host_alloc` 暂存化
   - → `cp_submit_cl_`：写 `cp_ring_`（CP 可见主机内存）、敲 `CP_Q_TAIL`、轮询 `CP_Q_SEQNUM`
   - → CP DMA 引擎（设备侧，本讲不涉及实现）按命令搬运，VA 经 MMU 翻译成 PA，落到设备 DRAM。

3. **回答一个综合问题**：假设你在 SimX 上用默认配置（VM 开启）运行 demo，主机 `vx_mem_alloc` 分配了输入 buffer A。请按顺序回答：
   - A 的 `dev_addr_` 是 PA 还是 VA？
   - 这个地址是怎么产生出来的（涉及哪两个分配器/管理器）？
   - 随后 `vx_copy_to_dev(A, ...)` 时，CP DMA 收到的目标地址是什么？它如何落到真实 DRAM？

   **参考答案**：
   - 是 VA。
   - `Device::mem_alloc` 先从 `global_mem_` 分配一个 PA，再调 `vm_mgr_->phy_to_virt_map` 铸造 VA 并安装页表项，把地址改写成 VA（[device.cpp:719-725](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L719-L725)）。
   - CP DMA 收到的目标地址是这个 VA；它用自己的 MMU（SATP 指向 `VMManager` 构建的页表）把 VA 翻译成 PA，再 DMA 到 DRAM 对应位置。

> 提示：这张图是后续 u3-l3（驱动后端与 stub 分发）、u3-l4（启动流程与 .vxbin 加载）的基础，建议保存。

## 6. 本讲小结

- **`vx_device_h` 是 `vx::Device*`**：它持有后端抽象 `Platform`、纯主机侧的地址分配器 `global_mem_`/`pinned_mem_`，以及可选的 `VMManager`。「分配设备内存」只是分发地址数字，不碰真实存储。
- **`vx_buffer_h` 是 `vx::Buffer*`**：一个瘦包装器，把「设备地址 + 大小 + 标志」打包成引用计数对象；VM 开启时其 `dev_addr_` 是 VA。
- **`mem_alloc` 的 VA/PA 分叉**：VM 开启且非 PHYS 时，`phy_to_virt_map` 铸造 VA 并把返回地址改写成 VA；`mem_free` 则要先把 VA 翻译回 PA 才能归还。
- **`VMManager` 维护影子页表**：读 PTE 走主机 memcpy，写 PTE 按页批量 `flush` 到设备，模仿主流 GPU 驱动的高效模式；页表区在 `cp_init` 时被预留并恒等映射。
- **`copy_to_dev` 的数据通路**：数据先 memcpy 进 CP 可见主机暂存，再以一条 `CMD_MEM_WRITE` 写进 CP 环，由 CP DMA 引擎搬运；主机运行时从不直接写设备 DRAM。
- **贯穿主线**：CP 是唯一的控制通路与 DMA 引擎，Device/Buffer/VMManager 三者都围绕「给 CP 准备命令与地址」展开。

## 7. 下一步学习建议

- **u3-l3（驱动后端与 stub 动态分发）**：本讲的 `Platform` 接口和 `CallbacksAdapter` 是如何由 `libvortex.so` 的 stub 按 `$VORTEX_DRIVER` 动态选择的，下一讲详述。
- **u3-l4（主机→设备启动流程与 .vxbin 加载）**：本讲只讲了内存与拷贝；`vx_start` 如何把内核 PC 和参数程序进 KMU，以及 `module.cpp` 如何解析 `.vxbin` 符号表，下一讲展开。
- **延伸阅读源码**：若你对 CP 的命令执行感兴趣，可提前浏览 `sim/simx` 下的 CP/KMU 相关实现（u11-l3 会系统讲解）；若对页表遍历的硬件侧感兴趣，可看 `sim/simx/mem/mmu.cpp`（u11-l1）。
