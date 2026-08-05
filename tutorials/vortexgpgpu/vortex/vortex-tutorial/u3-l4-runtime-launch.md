# 主机→设备启动流程与 .vxbin 加载

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清一个 `.vxbin`（Vortex 设备可执行镜像）在磁盘上的字节布局，尤其是可选的 `VXSYMTAB` 多入口符号表尾部（footer）。
- 跟踪 `module.cpp` 加载器如何「从尾部嗅探 magic、向前解析」恢复出「名字→PC」符号表，并理解它与无 footer 的单入口格式如何向后兼容。
- 掌握 `vx_module_get_kernel(name)` 如何把一个名字解析成设备入口 PC。
- 看懂一次 kernel launch 如何从主机 API 一路走到命令处理器（CP）的命令环，再被 KMU（Kernel Management Unit）消费：包括 `VX_DCR_KMU_*` 描述符的写入、`CMD_LAUNCH` 的编码，以及 KMU 如何把这些字段装进每个 CTA 的派发请求。
- 画出「主机加载 `.vxbin` → KMU 收到启动 PC 与参数」的完整数据流。

本讲只讲**主机侧加载与启动编程**，以及 KMU 如何接收这些字段。至于「`__vx_cta_entry` 设备侧 prologue 如何把 PC/参数派发给具体 kernel」，那是 [u4-l1 内核运行时启动与入口模型](u4-l1-kernel-startup.md) 的主题，本讲只在衔接处点到为止。

## 2. 前置知识

本讲承接 [u3-l2 设备、缓冲区与内存管理](u3-l2-runtime-device-mem.md) 与 [u3-l3 驱动后端与 stub 动态分发](u3-l3-runtime-drivers.md)。在读本讲前，请确认你已理解以下概念：

- **CP（命令处理器）是主机与设备之间唯一的控制通路与 DMA 引擎**。主机从不直接写设备内存，所有「写显存 / 写 DCR / 启动 kernel」都被编码成命令，塞进位于主机 pinned 内存里的命令环，由 CP 取走执行（详见 `docs/designs/command_processor.md`）。
- **DCR（Device Control Register，设备控制寄存器）** 是设备侧的「配置寄存器总线」。启动一个 kernel 前，主机要把「程序基址、kernel 入口 PC、参数指针、grid/block/cluster 维度」等一组描述字段写进一组名为 `VX_DCR_KMU_*` 的 DCR。
- **KMU（Kernel Management Unit）** 是设备侧负责「把一次 launch 拆成若干 CTA（Cooperative Thread Array）并派发给各个 core」的单元。它读取上述 DCR，在收到 `CMD_LAUNCH` 后开始派发。
- **CTA / warp / thread** 的层次：一个 kernel launch = 一个 grid；grid 拆成多个 CTA（block）；每个 CTA 拆成多个 warp；每个 warp 含若干 thread。这部分在 [u1-l1](u1-l1-project-overview.md) 已建立心智模型。
- **设备虚拟地址 vs 物理地址**：主机拿到的是设备虚拟地址（VA），由 CP 的 MMU 翻译成物理地址（PA）落进 DRAM。本讲不展开页表，只关注地址数字如何被传递。

> 关键认知：在 Vortex 里，「上传 kernel 镜像」和「启动 kernel」是**两件分开的事**。前者只是把一段字节搬进设备内存并记下它的地址；后者才真正向 KMU 编程「从哪个地址开始执行、入口在哪、参数在哪、跑多大网格」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [sw/runtime/common/module.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp) | **本讲主角**。`.vxbin` 加载器：解析 `VXSYMTAB` 符号表，提供 `Module`（已加载镜像+符号表）与 `Kernel`（命名入口，PC 已缓存）两类对象。 |
| [sw/runtime/common/legacy_utils.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp) | 旧式同步上传 `vx_upload_kernel_file` / `vx_upload_kernel_bytes`：只读 16 字节头，把整段二进制当裸缓冲上传，不解析符号表。 |
| [sw/runtime/common/legacy_runtime.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp) | 旧式同步启动 `vx_start`：手动写一整组 KMU DCR，再发一条裸 `CMD_LAUNCH`。 |
| [sw/runtime/common/queue.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp) | 异步 `vx_enqueue_launch`：从 `Kernel` 句柄推导出 STARTUP_ADDR / KERNEL_ENTRY，把完整 KMU 描述符写进 CP 环。 |
| [sw/runtime/common/device.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp) | CP 命令的编码与提交：`cp_init`（建环）、`cp_submit_dcr_write`（CMD_DCR_WRITE）、`cp_submit_launch`（CMD_LAUNCH）、`cp_submit_cl_`（写环+敲门铃+轮询）。 |
| [sim/simx/kmu/kmu.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp) | SimX 的 KMU 模型：`dcr_write` 把字段存进 `PC_`/`entry_`/`param_`/各维度；`start` 置位运行标志；`step` 为每个 CTA 装填一个派发请求。 |
| [sw/kernel/src/vx_start.S](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S) | 设备侧统一入口 `__vx_cta_entry`：本讲只看它如何消费 KMU 传来的入口 PC 与参数。 |
| [VX_types.toml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml) | `VX_DCR_KMU_*` 寄存器编号的真相来源（软硬件共享 ABI）。 |
| [docs/designs/kernel_entry_and_dispatch.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/kernel_entry_and_dispatch.md) | 多入口 `.vxbin` 与统一入口模型的设计文档。 |
| [docs/designs/command_processor.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md) | CP 架构、命令格式、提交路径。 |

---

## 4. 核心概念与源码讲解

### 4.1 `.vxbin` 的磁盘布局：从单入口到多入口符号表

#### 4.1.1 概念说明

`.vxbin` 是 Vortex 的设备可执行文件——经过 RISC-V clang 编译、链接、再用 `sw/kernel/scripts/vxbin.py` 打包后的产物。主机运行时拿到的是一个**纯字节流**，需要自己解析出「这段代码该放到设备内存的哪里」「有几个命名入口、各自 PC 是多少」。

历史上 Vortex 只有「单入口」模型：一个 `.vxbin` 就是一个 kernel，入口固定在镜像最低地址（`min_vma`）。后来为了支持 OpenCL（PoCL）等多 kernel 程序——**一个二进制里塞多个命名 kernel**——引入了 `VXSYMTAB` 符号表。设计的关键约束是：**没有多入口符号的 `.vxbin` 必须与旧格式逐字节相同**，这样老的单 kernel 回归测试无需任何改动。

#### 4.1.2 核心流程

`.vxbin` 的磁盘布局如下（头部固定 16 字节，尾部符号表可选）：

```
偏移        内容
───────────────────────────────────────────────────────────
0x00        min_vma        (8 字节, 小端)   镜像最低虚拟地址
0x08        max_vma        (8 字节, 小端)   镜像最高虚拟地址(不含)
0x10        image bytes ...                 长度 = bin_sz
            ─── 以下为可选的 VXSYMTAB 尾部 ───
            string blob                     所有符号名紧凑存放, NUL 分隔
            entries: N × 16 字节            每条 {name_off:4, name_len:2, pad:2, pc:8}
            n_symbols   (4 字节, 小端)
EOF         magic       (8 字节 'VXSYMTAB')
```

加载器的解析策略是**「从文件末尾嗅探 magic，向前倒推」**：

1. 读最后 8 字节，若等于 `VXSYMTAB` → 存在尾部；否则是旧式单入口镜像。
2. 若有尾部：从 `EOF-12` 读出 `n_symbols`；其上方 `n_symbols × 16` 字节是入口表；入口表上方是字符串 blob（blob 大小由各入口的 `name_off + name_len` 的最大值反推）。
3. 真正的二进制长度为：

\[
\text{bin\_sz} = \text{file\_sz} - 16 - \text{footer\_total}
\]

其中 \(\text{footer\_total} = 12 + n\_symbols \times 16 + \text{string\_blob\_size}\)。

4. 若无尾部：直接合成一个 `"main" → min_vma` 的单入口，行为与旧格式完全一致。

#### 4.1.3 源码精读

布局定义写在 `module.cpp` 顶部的注释里，是理解全篇的钥匙：

- [module.cpp:L14-L27](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L14-L27)：`.vxbin` 布局与「嗅探 magic、向前解析、无 footer 则回退到 main@min_vma」的加载策略说明。

加载入口 `Module::load_bytes` 先读 16 字节头、校验 `max_vma > min_vma`：

- [module.cpp:L75-L78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L75-L78)：读出 `min_vma`/`max_vma`，算出运行时区间 `rt_sz = max_vma - min_vma`。

随后是尾部嗅探的核心：

- [module.cpp:L82-L111](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L82-L111)：比对最后 8 字节是否为 `VXSYMTAB`；若是，读 `n_symbols`（在 `EOF-12`），计算入口表大小，再扫描所有入口的 `(name_off, name_len)` 反推字符串 blob 大小，累加出 `footer_total`。注意这里有一道安全检查：若 `footer_total > size - 16`（尾部比可用空间还大），说明这不是合法符号表，则丢弃、回退到单入口。

算出 `bin_sz` 后，把镜像reserve 到设备的 `[min_vma, max_vma)` 区间并上传：

- [module.cpp:L113-L140](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L113-L140)：`bin_sz = size - 16 - footer_total`；`Buffer::reserve` 在设备地址簿里占住该 VMA 区间；`.text/.rodata` 标记只读、`.bss` 区间标记读写；用 `dev->dev_write` 同步上传二进制并把 BSS 清零。这里走的是 CP 的 DMA（见 4.3），但 `Module` 本身是一个**纯同步原语**——加载镜像不需要队列。

最后把符号表填进 `Module::symbols_`：

- [module.cpp:L145-L170](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L145-L170)：有 footer 时，遍历 `n_symbols` 条入口，按 `(name_off, name_len)` 从字符串 blob 切出名字、读出 `pc`，压入 `symbols_`。**兼容性关键**在第 165-167 行：如果整个镜像只有一个入口、且它的名字不叫 `"main"`，就额外补一条 `{"main", min_vma}`——这样无论编译器是否为单 kernel 镜像生成了入口桩，调用方用约定俗成的 `"main"` 名字都能解析到。无 footer 时（第 168-170 行）直接合成 `{"main", min_vma}`。

> 这就是「单入口与多入口兼容设计」的全部秘密：多入口靠 footer，单入口靠「自动别名 main@min_vma」或「无 footer 回退」。对一个单 kernel 回归测试镜像，无论走哪条路，`vx_module_get_kernel(mod, "main")` 都能拿到 `min_vma`。

#### 4.1.4 代码实践：亲眼看到 VXSYMTAB 尾部

1. **实践目标**：用一个真实 `.vxbin` 验证上面的字节布局。
2. **操作步骤**：
   - 先按 [u1-l4](u1-l4-first-run.md) 在 `build/` 目录用 `ci/blackbox.sh` 跑通一个程序（例如 `./ci/blackbox.sh --driver=simx --app=demo`），让构建系统生成 `.vxbin`。
   - 在构建产物里找到设备镜像文件（通常位于 `tests/regression/<app>/build/` 下，文件名为 `<app>.vxbin` 或 `kernel.vxbin`，**具体路径待本地确认**）。
   - 用 `xxd` 查看头 16 字节与末尾 16 字节：
     ```bash
     xxd <app>.vxbin | head -1          # 头部: min_vma(8) + max_vma(8)
     xxd <app>.vxbin | tail -1          # 尾部: ... n_symbols(4) + 'VXSYMTAB'(8)
     ```
   - 用 `od` 或 `stat` 看文件大小，手算 `bin_sz`。
3. **需要观察的现象**：
   - 头 8 字节是一个合理的低地址（`min_vma`，例如 `0x80000000` 附近）。
   - 末 8 字节若是 ASCII `VXSYMTAB` → 该镜像带符号表；若是普通代码字节 → 旧式单入口镜像（回归测试多为这种）。
4. **预期结果**：能从尾部读出 `n_symbols`。对单 kernel 的 `demo`，`n_symbols` 应为 1（其名字可能是 `main` 或编译器生成的入口桩名）；对 `tests/regression/multikernel`，应为 3（`add_k`/`mul_k`/`acc_k`）。
5. 若找不到 `.vxbin` 或无法确认其路径，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么加载器要「从文件末尾向前解析」，而不是在头部记录符号表偏移？

> **答案**：为了让「没有符号表的旧式单入口镜像」与多入口镜像**逐字节兼容**。头部固定为 16 字节（`min_vma`/`max_vma`），旧工具生成的镜像头部之后直接就是二进制；若在头部加一个「符号表偏移」字段，就会破坏旧格式。把符号表放在可选的尾部、用末尾 magic 探测，则无 magic 的旧文件天然被当作单入口处理。

**练习 2**：`footer_total` 的安全检查（`footer_total <= size - 16`）是为了防什么？

> **答案**：防止「文件末尾恰好出现 `VXSYMTAB` 这 8 个字节、但并不是真正的符号表」时被误判。若由 `n_symbols` 反推出的尾部比可用空间还大，说明这只是巧合，应丢弃尾部、回退为单入口，而不是读越界。

---

### 4.2 `module.cpp` 加载器：名字 → PC 解析（`vx_module_get_kernel`）

#### 4.2.1 概念说明

`module.cpp` 引入了两个对象类型，把「加载镜像」和「解析入口」解耦：

- **`Module`** = 一个已加载的 `.vxbin`：持有设备上的镜像 `Buffer`、基地址 `base_addr_`、以及一张 `symbols_`（名字→PC）表。
- **`Kernel`** = Module 内一个命名入口：缓存了那个入口的 PC，并持有对所属 Module 的引用（保证镜像存活）。

这套对象模型对应公开 API：`vx_module_load_file/bytes` 产出 `vx_module_h`，`vx_module_get_kernel(mod, name)` 产出 `vx_kernel_h`。它取代了旧式「kernel 就是一段裸缓冲」的模型，使得**一个镜像里的多个 kernel 可以按名字分别启动**。

#### 4.2.2 核心流程

一次「按名字启动」的加载-解析流程：

```
vx_module_load_file(path)
        │  Module::load_file → load_bytes
        │   1. 读 16B 头 + 嗅探 VXSYMTAB 尾部
        │   2. reserve [min_vma, max_vma) 并上传二进制 + 清零 BSS
        │   3. 把符号表填进 Module::symbols_
        ▼
vx_module_h  ──── vx_module_get_kernel(mod, "add_k")
        │   Module::get_kernel:
        │   - 查 kernel_cache_（命中即返回）
        │   - 否则线性扫描 symbols_，名字匹配则用该 PC 创建 Kernel 并缓存
        ▼
vx_kernel_h  ──── vx_enqueue_launch(... kernel=h ...)   # 4.3 展开
```

`get_kernel` 带了一张 `kernel_cache_`：同一个名字反复查找只创建一次 `Kernel` 对象，缓存里存的是**非拥有型裸指针**（`Kernel` 的析构会把自己从缓存里摘除），对象所有权走引用计数。

#### 4.2.3 源码精读

`get_kernel` 的名字→PC 解析：

- [module.cpp:L176-L200](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L176-L200)：先加锁查 `kernel_cache_`；未命中则线性遍历 `symbols_`，找到名字相同的条目就用其 `pc` 调 `Kernel::create`，把新 Kernel 的裸指针塞进缓存，并把那唯一的引用计数返回给调用方。找不到名字返回 `VX_ERR_INVALID_VALUE`。

`Kernel` 对象与 Module 的引用关系：

- [module.cpp:L206-L233](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L206-L233)：`Kernel` 构造时 `retain()` 所属 Module（保证镜像 `Buffer` 不被提前释放），析构时把自己从缓存的 map 里摘除再 `release()` Module。`Kernel::create` 只是 `new` 一个带 PC 的 Kernel。

对应的 C 入口：

- [module.cpp:L258-L268](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L258-L268)：`vx_module_load_file` 把文件整段读进内存后委托给 `load_bytes`。
- [module.cpp:L294-L304](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L294-L304)：`vx_module_get_kernel` 转调 `Module::get_kernel`。

#### 4.2.4 代码实践：对比「Module 多入口」与「旧式裸缓冲」两条加载路径

1. **实践目标**：用两个真实测试，看清「按名字解析入口」与「旧式裸缓冲」的差别。
2. **操作步骤**：
   - 读 [tests/regression/multikernel/main.cpp:L89-L94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp#L89-L94)：它先 `vx_module_load_file`，再用 `vx_module_get_kernel(mod, "add_k"/"mul_k"/"acc_k")` 取出**三个**命名 kernel，随后分别 `vx_enqueue_launch`（见 [:L146-L148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp#L146-L148)）。这正是多入口符号表存在的意义。
   - 再读 [tests/regression/demo/main.cpp:L204-L205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L204-L205)：`demo` 同样走 Module 路径，但只取 `"main"` 这一个入口。
   - 对照旧式路径 [legacy_utils.cpp:L25-L72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp#L25-L72) 的 `vx_upload_kernel_bytes`：它只读头 16 字节、把 `size-16` 当作整段二进制上传、返回一个裸 `vx_buffer_h`——**完全没有符号表概念**，调用方拿到的「kernel」只是一个缓冲句柄。
3. **需要观察的现象**：`demo` 与 `multikernel` 都用 `vx_module_*` API；旧式 `vx_upload_kernel_*` 只在更老的样例里出现。
4. **预期结果**：你能用一句话说清二者区别——Module 路径「解析符号表、按名字给入口」，旧式路径「整段上传、入口固定 = 缓冲地址」。
5. 若想看运行效果，可在 SimX 上跑 `multikernel`（命令**待本地确认**，例如 `./ci/blackbox.sh --driver=simx --app=multikernel`）。

#### 4.2.5 小练习与答案

**练习 1**：`kernel_cache_` 存的是裸指针而非引用，为什么不会内存泄漏或悬垂？

> **答案**：`Kernel` 析构时（[module.cpp:L211-L227](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L211-L227)）会主动遍历缓存、把自己那条擦掉。所以只要某个 Kernel 的引用计数归零、对象被销毁，缓存里就不会再留下指向它的悬垂指针；反过来缓存也不额外拥有 Kernel，不会阻止其释放。

**练习 2**：为什么 `Kernel` 要 `retain()` 它的 `Module`？

> **答案**：`Kernel` 的 PC 来自 `Module::symbols_`，而启动时还要用到 Module 镜像 `Buffer` 的基地址（见 4.3 的 `program_pc`）。只要还有一个 Kernel 句柄在外，它底层的镜像就必须存活，所以 Kernel 持有 Module 的引用计数。

---

### 4.3 `vx_start` / `vx_enqueue_launch` → KMU：把 kernel + arguments 程序进命令处理器

#### 4.3.1 概念说明

「加载镜像」之后，第二件事是**启动**：告诉 KMU「从哪个地址开始跑、入口 kernel 在哪、参数块在哪、grid/block/cluster 多大」。这些信息通过一组 `VX_DCR_KMU_*` 设备控制寄存器传递，它们的编号定义在 `VX_types.toml`（软硬件共享 ABI）：

| DCR | 编号 | 含义 |
|---|---|---|
| `STARTUP_ADDR0/1` | 0x010/0x011 | 程序基址（每个 warp 开始执行的地址，即 `__vx_cta_entry` 所在） |
| `KERNEL_ENTRY0/1` | 0x012/0x013 | 所选 kernel 的功能入口 PC（每 CTA 经 `VX_CSR_CTA_ENTRY` 读回） |
| `STARTUP_ARG0/1` | 0x014/0x015 | kernel 参数块指针（每 CTA 经 `VX_CSR_MSCRATCH` 读回，进 `a0`） |
| `BLOCK_DIM_X/Y/Z` | 0x016–0x018 | 一个 CTA(block) 的维度 |
| `GRID_DIM_X/Y/Z` | 0x019–0x01B | grid 维度（CTA 总数 = 三轴之积） |
| `LMEM_SIZE` | 0x01C | 每个 block 的本地内存需求 |
| `BLOCK_SIZE` | 0x01D | 每个 block 的线程总数 |
| `WARP_STEP_X/Y/Z` | 0x01E–0x020 | 把 block 切成 warp 时的步长 |
| `CLUSTER_DIM_X/Y/Z` | 0x021–0x023 | cluster 形状（保证同驻一个 core 的 CTA 分组） |

> 完整编号见 [VX_types.toml:L64-L83](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L64-L83)。注意 64 位地址/PC 都拆成 `Lo/Hi` 两条 32 位 DCR 写入。

主机侧有**两套等价的启动 API**，最终都汇入「写一组 KMU DCR + 发一条 `CMD_LAUNCH`」：

- **旧式同步 `vx_start(dev, kernel_buf, args_buf)`**：把 kernel 当作裸缓冲，手动写整组 DCR，再发裸 launch。
- **异步 `vx_enqueue_launch(... kernel=h ...)`**：从 `Kernel` 句柄推导出 STARTUP_ADDR / KERNEL_ENTRY，在 worker 线程里把完整描述符写进 CP 环。

二者之所以能共存，是因为 `vx_enqueue_launch` 留了**「遗留逃生舱」**：当 `info->kernel == NULL` 且 `ndim == 0` 时，它假定调用方已经自己写好那些 DCR，只负责发一条 `CMD_LAUNCH`。`vx_start` 用的正是这个逃生舱。

#### 4.3.2 核心流程

一次 launch 从主机到 KMU 的完整通路：

```
主机 API
   │
   │  vx_start: 直接写 ~18 条 CMD_DCR_WRITE(KMU_*) + 1 条 CMD_LAUNCH
   │  vx_enqueue_launch(kernel=h): 由 h 推导 PC，同样写 KMU DCR + CMD_LAUNCH
   ▼
Device::cp_submit_dcr_write / cp_submit_launch      (device.cpp)
   │  把每条命令编码进一个 64B cache line (CL)
   ▼
Device::cp_submit_cl_ → cp_ring_append_             (device.cpp)
   │  1) memcpy 命令 CL 进主机 pinned 内存里的命令环
   │  2) 写 CP_Q_TAIL_LO/HI 敲门铃 (doorbell)
   │  3) 忙轮询 CP_Q_SEQNUM 直到该命令退休
   ▼
CP (命令处理器, RTL: VX_cp_core / 仿真: cmd_processor.cpp)
   │  取指→解包→分发到 KMU 仲裁器
   │  CMD_DCR_WRITE → 写 KMU DCR；CMD_LAUNCH → 脉冲 KMU start
   ▼
KMU (sim/simx/kmu/kmu.cpp)
   │  dcr_write: 把字段存进 PC_/entry_/param_/各维度
   │  start:     置 running_=true
   │  step:      为每个 CTA 装填一个 kmu_req_t {PC, entry, param, block_idx, dims...}
   ▼
每个 core 的 __vx_cta_entry (vx_start.S)
      csrr s11, VX_CSR_CTA_ENTRY   # 取 kernel 入口 (=entry_)
      csrr a0,  VX_CSR_MSCRATCH    # 取参数指针 (=param_)
      jalr ra, s11                 # 派发到 kernel
```

命令编码要点（来自 `docs/designs/command_processor.md` §2）：

- **`CMD_DCR_WRITE`（opcode 0x04，20 字节）**：`{header, arg0=DCR addr, arg1=DCR value}`。
- **`CMD_LAUNCH`（opcode 0x06，12 字节）**：`{header, arg0=unused}`，脉冲 KMU 的 start 并等它排空。
- 多条命令紧凑打包进 64 字节的 CL（最多 5 条/行），CP 一次取一行来解码。

#### 4.3.3 源码精读

**(a) 异步路径：`vx_enqueue_launch` 如何从 Kernel 推导并编程 KMU**

[queue.cpp:L265-L449](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L265-L449) 是异步 launch 的全部逻辑。关键点：

- [queue.cpp:L293-L298](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L293-L298)：注释点明三种「遗留逃生舱」——`kernel==NULL`（自填 PC DCR）、`args_host==NULL`（自填 ARG DCR）、`ndim==0`（自填 grid/block DCR）。`vx_start` 同时用了前两种和第三种。
- [queue.cpp:L322-L334](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L322-L334)：在 worker 线程内由 `Kernel` 句柄推导两个 PC——`kernel_pc`（→KERNEL_ENTRY：所选 kernel 的功能入口）与 `program_pc`（→STARTUP_ADDR：镜像基址，即 `__vx_cta_entry` 所在）。两者都按 XLEN 拆成 `Lo/Hi`。
- [queue.cpp:L391-L435](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L391-L435)：用 `WR(addr,val)` 宏（宏展开为 `cp_submit_dcr_write`）依次写 STARTUP_ADDR、KERNEL_ENTRY、STARTUP_ARG（参数块先 `args_slot_acquire` 暂存到设备 scratch 再写其地址）、BLOCK_DIM、GRID_DIM、LMEM_SIZE、BLOCK_SIZE、WARP_STEP、CLUSTER_DIM；最后 `cp_submit_launch()` 发 `CMD_LAUNCH`。这一段就是「把 kernel+arguments 程序进 KMU」的主干。

**(b) 旧式同步路径：`vx_start`**

[legacy_runtime.cpp:L158-L234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L158-L234) 的 `vx_start` 是同步封装。要点：

- [legacy_runtime.cpp:L166-L183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L166-L183)：先排空上一次遗留的 launch；查询 `NUM_CORES/NUM_THREADS/NUM_WARPS`；用 `prepare_kernel_launch_params` 算出 block_size 与 warp_step（grid 固定为 `num_cores`，block 取整 warp 宽度）。
- [legacy_runtime.cpp:L194-L220](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L194-L220)：把 `kernel->dev_address()`（= 镜像基址）当 `STARTUP_ADDR`、`args->dev_address()` 当 `STARTUP_ARG`，连同维度，构造 `kmu_writes[]` 数组，逐条 `vx_enqueue_dcr_write` 写进队列。
- [legacy_runtime.cpp:L222-L233](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_runtime.cpp#L222-L233)：构造一个 `kernel=NULL, ndim=0` 的 `vx_launch_info_t` 调 `vx_enqueue_launch`——即触发「逃生舱」，只发一条 `CMD_LAUNCH`。

配套的 `vx_upload_kernel_file` 见 [legacy_utils.cpp:L74-L97](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp#L74-L97)（读文件）转 [legacy_utils.cpp:L25-L72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/legacy_utils.cpp#L25-L72)（`vx_upload_kernel_bytes`：reserve + copy_to_dev）。**注意**：旧式上传只读 16 字节头，假定 `size-16` 就是整段二进制——它不识别 `VXSYMTAB` 尾部，因此只适用于无尾部的单 kernel 镜像。

**(c) CP 命令编码与提交（device.cpp）**

- [device.cpp:L457-L500](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L457-L500) `cp_submit_dcr_write`：按 `CMD_DCR_WRITE` 在线布局 `{opcode=0x04, addr, value}` 填一个 64B CL。
- [device.cpp:L502-L520](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L502-L520) `cp_submit_launch`：发 `CMD_LAUNCH`(0x06)，紧跟一条 `CMD_CACHE_FLUSH`（保证 kernel 写回对主机可见），最后排空 COUT 控制台环。
- [device.cpp:L393-L455](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L393-L455) `cp_submit_cl_`：三步——`cp_ring_append_`（[:L336-L348](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L336-L348) 把 CL memcpy 进环并预留 seqnum）→ 写 `CP_Q_TAIL_LO/HI` 敲门铃 → 忙轮询 `CP_Q_SEQNUM` 直到 `≥ target`。release 内存屏障保证 CP 不会读到半写的环表项。
- [device.cpp:L232-L264](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L232-L264) `cp_init`：在 `vx_dev_open` 时分配环/head/cmpl 三块主机 pinned 内存，编程队列 0 的基地址与大小，置 `CP_REG_CTRL=1`。

**(d) KMU 如何接收（sim/simx/kmu/kmu.cpp）**

- [kmu.cpp:L47-L68](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L47-L68) `dcr_write`：把各 `VX_DCR_KMU_*` 存进 `PC_`/`entry_`/`param_`/`block_dim_`/`grid_dim_`/`lmem_size_`/`block_size_`/`warp_step_`/`cluster_dim_`。64 位字段用 `Lo/Hi` 两写拼合。
- [kmu.cpp:L73-L86](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L73-L86) `start`：校验维度合法后置 `running_=true`，重置 CTA 游标。
- [kmu.cpp:L88-L118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L88-L118) `step`：为每个 CTA 装填一个 `kmu_req_t`，其中 `req->PC=PC_`、`req->entry=entry_`、`req->param=param_`，连同该 CTA 的 `block_idx`、维度、cluster 内偏移等。core 收到请求后，会把 `entry`/`param` 分别灌进 `VX_CSR_CTA_ENTRY`/`VX_CSR_MSCRATCH`，交给 prologue。

**(e) 设备侧 prologue 如何消费（衔接，详见 u4-l1）**

- [vx_start.S:L119-L130](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_start.S#L119-L130)：每个 CTA 在镜像基址（`STARTUP_ADDR`）的 `__vx_cta_entry` 进入；prologue 完成寄存器/TLS/全局构造后，在「每 CTA 派发窗口」里 `csrr s11, VX_CSR_CTA_ENTRY` 取 kernel 入口、`csrr a0, VX_CSR_MSCRATCH` 取参数指针，再 `jalr ra, s11` 派发到该 kernel。注释强调调度器会「回卷 PC 跨过这 20 字节窗口」来为同一 warp 槽重复进入，所以 `jalr` 必须用 `.option norvc` 强制 4 字节。

> 串起来：主机写的 `STARTUP_ADDR` → KMU 的 `PC_` → 每个 CTA 的起始执行地址（`__vx_cta_entry`）；主机写的 `KERNEL_ENTRY` → KMU 的 `entry_` → 每 CTA 的 `VX_CSR_CTA_ENTRY` → `s11` → `jalr` 目标；主机写的 `STARTUP_ARG` → KMU 的 `param_` → 每 CTA 的 `VX_CSR_MSCRATCH` → `a0`（kernel 的第一个参数）。

#### 4.3.4 代码实践：跟踪 `vx_upload_kernel_file → vx_start` 调用链

1. **实践目标**：把本讲知识点串成一条完整数据流，亲手「走一遍」从主机加载到 KMU 收到 PC。
2. **操作步骤**：
   - 在仓库里用编辑器跳转，按顺序打开：`legacy_utils.cpp:vx_upload_kernel_file` → `vx_upload_kernel_bytes` → `legacy_runtime.cpp:vx_start` → `queue.cpp:enqueue_launch`（worker lambda）→ `device.cpp:cp_submit_dcr_write` → `device.cpp:cp_submit_cl_` → `kmu.cpp:dcr_write` → `kmu.cpp:step`。
   - 在一张纸上（或文本里）画出三列对照表：
     | 主机侧字段 | 对应 KMU DCR | KMU 成员 / 设备 CSR |
     |---|---|---|
     | `program_pc`（Module 基址） | STARTUP_ADDR0/1 | `PC_` / 起始执行地址 |
     | `kernel_pc`（`get_kernel` 解析的 PC） | KERNEL_ENTRY0/1 | `entry_` / `VX_CSR_CTA_ENTRY` |
     | `args_addr`（参数块设备地址） | STARTUP_ARG0/1 | `param_` / `VX_CSR_MSCRATCH` |
     | grid/block/cluster 维度 | GRID_DIM/BLOCK_DIM/CLUSTER_DIM/... | `grid_dim_`/`block_dim_`/`cluster_dim_` |
   - 验证你的表：在 `queue.cpp` 的 `WR(...)` 序列里找到每一项对应的 DCR 名，再到 `kmu.cpp` 的 `dcr_write` 找到它存进哪个成员。
3. **需要观察的现象**：主机侧的每一个 `WR(addr,val)` 都能在 `kmu.cpp` 的 `switch` 里找到同名 `case`，形成一一对应。
4. **预期结果**：你得到一张「主机 API → KMU DCR → KMU 成员 → 设备 CSR」的完整映射图，并能解释为何 64 位地址要拆两条 DCR。
5. 命令运行结果**待本地验证**（若要在 SimX 上实测，可用 `--debug` 生成 trace，在 trace 里观察 KMU 收到的 `kmu_req_t` 字段）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 64 位的 `STARTUP_ADDR` 要拆成 `STARTUP_ADDR0/1` 两条 32 位 DCR？

> **答案**：DCR 总线是 32 位数据宽度的（`CMD_DCR_WRITE` 的 value 字段是 32 位，见 [device.cpp:L489-L499](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L489-L499)）。要传一个 XLEN=64 的地址，只能分两次写：`ADDR0` 放低 32 位、`ADDR1` 放高 32 位，KMU 在 [kmu.cpp:L49-L50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/kmu/kmu.cpp#L49-L50) 用位拼合还原成完整 64 位。

**练习 2**：`vx_start` 调 `vx_enqueue_launch` 时为什么传 `kernel=NULL, ndim=0`？

> **答案**：因为 `vx_start` 已经**自己**把整组 KMU DCR（含 STARTUP_ADDR、STARTUP_ARG、维度）通过 `vx_enqueue_dcr_write` 写好了。把 `kernel=NULL` 与 `ndim=0` 传给 `vx_enqueue_launch`，就是触发它的「遗留逃生舱」（[queue.cpp:L293-L298](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L293-L298)），让它跳过所有 DCR 推导，**只发一条 `CMD_LAUNCH`**。

**练习 3**：`CMD_LAUNCH` 之后为什么还要跟一条 `CMD_CACHE_FLUSH`？

> **答案**：为了在 kernel 结束后让主机观察到**一致的**写回结果（ACQUIRE_MEM 模型）。见 [device.cpp:L510-L513](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/device.cpp#L510-L513)。在写穿（write-through）缓存配置下它是空操作，但默认保留这条纪律。

---

## 5. 综合实践

**任务**：以 `tests/regression/multikernel` 为对象，把「一个命名 kernel 从主机加载到 KMU 派发」的全过程讲清楚。

1. **阅读主机程序**：打开 [tests/regression/multikernel/main.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp)，定位 `vx_module_load_file`、三个 `vx_module_get_kernel`、三次 `vx_enqueue_launch`（[:L89-L94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp#L89-L94)、[:L146-L148](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/multikernel/main.cpp#L146-L148)）。
2. **回答三个问题**：
   - 这个 `.vxbin` 里有几个命名入口？它们的名字和 PC 是怎么进入 `Module::symbols_` 的？（提示：[module.cpp:L145-L170](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/module.cpp#L145-L170)）
   - 三次 launch 共用了同一个镜像基址（`STARTUP_ADDR`）还是各自不同？三次 launch 的 `KERNEL_ENTRY` 是否不同？为什么？（提示：[queue.cpp:L322-L406](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L322-L406)）
   - 参数块（`STARTUP_ARG`）每次 launch 之前都做了什么准备？launch 退休后又做了什么？（提示：[queue.cpp:L362-L382](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L362-L382) 与 [:L441-L442](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/queue.cpp#L441-L442)）
3. **画一张时序图**：横轴是时间，画出「load_file → get_kernel(add_k) → enqueue_launch(add_k) → CP 取指 → KMU dcr_write → CMD_LAUNCH → KMU start/step → core 进入 __vx_cta_entry」的顺序，标注每一步发生的文件与函数。
4. **（可选，待本地验证）** 在 SimX 上跑这个测试，加 `--debug` 生成 trace，在 trace 里找到 KMU 为 `add_k` 的第一个 CTA 装填的 `kmu_req_t`，确认其 `PC`/`entry`/`param` 与你画的图一致。

完成本任务后，你就把「主机→设备启动流程与 `.vxbin` 加载」这条主线彻底打通了。

## 6. 本讲小结

- `.vxbin` = 16 字节头（`min_vma`/`max_vma`）+ 二进制 + **可选的 `VXSYMTAB` 尾部**；加载器从文件末尾嗅探 magic、向前解析出「名字→PC」符号表，无尾部则回退为单入口 `"main"→min_vma`，从而与旧格式逐字节兼容。
- `module.cpp` 用 `Module`（镜像+符号表）/`Kernel`（命名入口，PC 已缓存）两类对象解耦「加载」与「解析入口」；`vx_module_get_kernel(name)` 线性查表 + 缓存命中。
- 启动 = 把一组 `VX_DCR_KMU_*`（STARTUP_ADDR / KERNEL_ENTRY / STARTUP_ARG / grid·block·cluster 维度）写进 KMU，再发一条 `CMD_LAUNCH`。
- 两套等价 API：异步 `vx_enqueue_launch(kernel=h)` 从 Kernel 句柄推导 PC；旧式同步 `vx_start` 手写整组 DCR 后用「遗留逃生舱」触发裸 launch。
- 所有命令都经 CP：在主机 pinned 内存环里编码成 64B CL → 敲 `Q_TAIL` 门铃 → 忙轮询 `Q_SEQNUM` 退休；CP 把 DCR 写与 launch 脉冲送到 KMU。
- KMU（`kmu.cpp`）把 DCR 存进 `PC_`/`entry_`/`param_`/各维度，`start` 置位、`step` 为每个 CTA 装填 `kmu_req_t`；设备侧 `__vx_cta_entry` 经 `VX_CSR_CTA_ENTRY`/`VX_CSR_MSCRATCH` 取入口与参数，派发到 kernel。

## 7. 下一步学习建议

- **[u4-l1 内核运行时启动与入口模型](u4-l1-kernel-startup.md)**：本讲只点到 `__vx_cta_entry` 的派发窗口，下一讲会完整拆解 `vx_start.S` 的统一 CTA prologue、`.weak kernel_main` 模型、`.vx_entry` 段与 `VXSYMTAB` footer 在 `vxbin.py` 里是如何生成的——也就是「设备侧如何使用主机送来的 PC」。
- **[u6-l1 Warp 调度器、CTA 派发与屏障](u6-l1-simx-scheduler.md)**：KMU 把 CTA 派发给 core 之后，scheduler/cta_dispatcher 如何把它拆成 warp、分配 PC 与 tmask。
- **[u11-l3 命令处理器与 KMU](u11-l3-command-processor.md)**：CP 的 RTL 实现（`VX_cp_core`/`VX_cp_launch`）与 KMU 的 RTL/SimX 对照，把本讲的 CP 命令环在硬件侧讲透。
- 想直接看「多入口」端到端效果，可继续阅读 `docs/designs/kernel_entry_and_dispatch.md` 与 `tests/regression/multikernel/`。
