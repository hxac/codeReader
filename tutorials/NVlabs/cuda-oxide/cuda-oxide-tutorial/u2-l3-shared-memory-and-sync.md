# 共享内存与同步

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 GPU 上「共享内存（shared memory）」与「全局内存」的区别，以及为什么大多数高性能内核都要先用共享内存做缓存。
- 用 `SharedArray<T, N>` 声明**静态**共享内存，并用 `unsafe` + `sync_threads` 的标准范式在块内协作。
- 用 `DynamicSharedArray<T, ALIGN>` 在**启动时**才决定共享内存大小，并理解它如何被划分成多个数组。
- 说出 `sync_threads()`（块级屏障）与 `threadfence` 系列（内存栅栏）的本质区别：前者是「等人」，后者是「让数据被看见」。
- 看懂 cuda-oxide 如何把这些「函数体只是 `unreachable!()`」的占位 intrinsic 在编译期替换成真正的 PTX 指令。

## 2. 前置知识

本讲承接 [u2-l2 线程索引与类型安全](u2-l2-thread-indexing-and-safety.md)。在继续之前，请确认你已经理解：

- **线程块（block / CTA）**：一个内核由若干个线程块组成，每个块又包含若干线程。本讲讨论的所有协作都发生在「同一个块内」。
- **`thread::index_1d()` 与 `ThreadIndex` 见证类型**：每个线程拿到一个唯一的、不可伪造的线程号。我们会用它作为共享内存的下标。
- **占位 intrinsic 模式**：cuda-device 里的很多函数（如 `sync_threads`、`threadfence`、`SharedArray::index`）的函数体只是 `unreachable!(...)`，并且标注 `#[inline(never)]`。**这些函数在 host 上永远不会被执行**——cuda-oxide 编译器在 MIR 阶段识别到这些调用点，把它们替换成对应的 NVVM/PTX 指令。这是理解本讲所有源码的钥匙。

### GPU 内存层级速览

为了让初学者有直觉，先用一张表对比三种最常打交道的存储：

| 存储 | 位置 | 速度 | 容量 | 可见范围 | 生命周期 |
|------|------|------|------|----------|----------|
| 寄存器 register | 每个 SM 片上 | 最快 | 极小（每线程） | 单个线程 | 单条指令 |
| 共享内存 shared | 每个 SM 片上 SRAM | 很快（≈寄存器带宽） | 小（每块约几十~两百多 KB） | **同一个块内的所有线程** | 内核运行期间 |
| 全局内存 global | 显存 DRAM（HBM/GDDR） | 慢（要经缓存） | 大（GB 级） | 所有线程、甚至宿主 | 跨内核持久 |

共享内存的关键性质是：**同一个块内的所有线程看到的是同一份共享内存**，不同块之间互相不可见，内核结束即失效。这正好是「块内协作」的天然载体——先把数据从慢的全局内存搬到快的共享内存，块内线程一起处理，再把结果写回全局内存。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [crates/cuda-device/src/shared.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs) | 定义 `SharedArray`（静态）与 `DynamicSharedArray`（动态）两种共享内存类型 |
| [crates/cuda-device/src/thread.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs) | 提供 `sync_threads()` 块级屏障 |
| [crates/cuda-device/src/fence.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/fence.rs) | 提供 `threadfence_block` / `threadfence` / `threadfence_system` 三档栅栏 |
| [crates/rustc-codegen-cuda/examples/sharedmem/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/sharedmem/src/main.rs) | `SharedArray` 的完整可运行示例 |
| [crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs) | `DynamicSharedArray` 的完整可运行示例 |
| [crates/rustc-codegen-cuda/examples/addressof_sharedarray/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/addressof_sharedarray/src/main.rs) | 一个最小共享内存内核，用于回归测试编译器对 `static mut SharedArray` 的地址处理 |
| [crates/mir-importer/src/translator/terminator/intrinsics/sync.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-importer/src/translator/terminator/intrinsics/sync.rs) | 编译器侧：把 `sync_threads()` / `threadfence*()` 调用翻译成 dialect 操作 |
| [crates/mir-lower/src/convert/intrinsics/basic.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-lower/src/convert/intrinsics/basic.rs) | 编译器侧：把 dialect 操作最终降级成 LLVM NVVM 内联函数 / PTX 内联汇编 |

## 4. 核心概念与源码讲解

### 4.1 SharedArray 静态共享内存

#### 4.1.1 概念说明

`SharedArray<T, N>` 是 cuda-oxide 对 CUDA 中 `__shared__ T arr[N]` 的封装。它有三个特点：

1. **编译期已知大小**：元素类型 `T` 和元素个数 `N` 都是 const generic，写死在类型里。
2. **存放于共享内存地址空间**：cuda-oxide 编译器认出这个类型，把它的存储分配到 LLVM 的 address space 3（GPU 共享内存）。
3. **声明为 `static mut`**：放在内核函数体内，用 `SharedArray::UNINIT` 初始化。

它解决的问题是：当一个块内的线程需要互相读取彼此写入的数据时（例如分块矩阵乘、块内归约），需要一个块内共享的「快黑板」。

#### 4.1.2 核心流程

使用 `SharedArray` 的标准三段式范式：

```
1. 每个线程把全局内存里自己负责的数据，写入共享内存[自己的 tid]
2. thread::sync_threads()         ← 等所有线程都写完
3. 每个线程从共享内存[任意下标]读取（此时可以读到别人写的值）
```

为什么第 2 步必须存在？因为线程是并发执行的，线程 A 读 `TILE[邻居]` 时，邻居线程可能还没把值写进去。`sync_threads()` 保证「块内所有线程都执行到这里后，才允许任何一个线程继续」，于是屏障之后的所有写入都对块内可见。

`SharedArray` 的类型定义本身只是一个零大小标记（ZST marker），真正的内存分配由编译器完成：

- 结构体里只有一个 `PhantomData<UnsafeCell<[T; N]>>`，没有真实字段。
- `#[repr(transparent)]` 表示它在内存布局上等同于那个 PhantomData（也就是什么都不占）。
- `Index` / `IndexMut` 的实现体是 `unreachable!()`——真正的加载/存储由编译器在 address space 3 上生成。

#### 4.1.3 源码精读

**类型定义**——注意 `ALIGN` 默认为 0（自然对齐），以及用 `PhantomData<UnsafeCell<...>>` 让类型成为 `!Sync`：

[crates/cuda-device/src/shared.rs:114-121](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L114-L121) —— `SharedArray` 只是一个 ZST marker，编译器靠识别这个类型名来分配共享内存。

**UNINIT 常量**——共享内存声明时的统一初值：

[crates/cuda-device/src/shared.rs:130-132](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L130-L132) —— 因为共享内存「内核启动时未初始化」，所以这个常量并不真正写入任何数据，只是满足 `static mut` 必须有初值的语法要求。

**Index / IndexMut**——这就是 `TILE[i]` 读写的入口，函数体永远不会在 host 上执行：

[crates/cuda-device/src/shared.rs:216-244](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L216-L244) —— 编译器把 `TILE[i]` 的取值替换成从 addrspace(3) 的加载、`TILE[i] = v` 替换成对 addrspace(3) 的存储。

**为什么是 `!Sync`**——这是个值得理解的健全性设计。文档明确解释：共享内存的并发安全依赖 `sync_threads`/`bar.sync` 这类硬件屏障，而 Rust 的类型系统看不到这些屏障，所以类型不能宣称自己是 `Sync`：

[crates/cuda-device/src/shared.rs:99-113](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L99-L113) —— 用 `UnsafeCell`（它本身 `!Sync`）来「毒化」自动 trait 推导。

**真实使用范式**——sharedmem 示例里的 `shared_test` 内核，展示了「写入 → sync_threads → 读邻居」的最小可运行闭环：

[crates/rustc-codegen-cuda/examples/sharedmem/src/main.rs:31-51](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/sharedmem/src/main.rs#L31-L51) —— 每个线程把 `data[gid]` 写进 `TILE[tid]`，屏障后读 `TILE[(tid+1)%256]`，即「右邻居」的值。

注意这里的 `unsafe` 边界：**共享内存的所有访问都在 `unsafe` 块里**。原因有二：内存未初始化、多线程并发访问（潜在数据竞争）。这与 u2-l2 讲的 `DisjointSlice` 不同——`DisjointSlice` 把越界/唯一性收进类型系统，而共享内存因为天然需要「别人写的值」，无法用借用检查表达，只能退回到 `unsafe` + 手动屏障。

> 💡 **回归测试视角**：示例目录里还有一个更小的 `addressof_sharedarray`，它专门用来盯住编译器对 `static mut SharedArray` 地址的处理（issue #54）。它只做「线程 0 写一个数、乘以权重、再读回来」：
> [crates/rustc-codegen-cuda/examples/addressof_sharedarray/src/main.rs:36-48](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/addressof_sharedarray/src/main.rs#L36-L48)。如果编译器在导出 LLVM IR 时把共享内存的 `addressof` 弄成悬空 SSA 引用，libNVVM 会在运行时校验失败——所以这个示例既是 demo 也是编译器回归哨兵。

#### 4.1.4 代码实践

**实践目标**：亲手跑通静态共享内存的「写入 → 屏障 → 读邻居」闭环。

**操作步骤**：

1. 进入示例目录，编译并运行（无需改动代码）：
   ```bash
   cargo oxide run sharedmem
   ```
2. 阅读输出，确认 `Test 1: Single SharedArray` 与 `Test 2: Dual SharedArray` 都打印 `✓`。
3. 把 `shared_test` 内核里的 `let neighbor_idx = (tid + 1) % 256;` 改成 `(tid + 5) % 256;`，重新运行，观察 `out[i]` 变成 `data[(i+5)%256]`。

**需要观察的现象**：

- 修改前：`out[0]` 应等于 `data[1]`（读右邻居 1）。
- 修改后：`out[0]` 应等于 `data[5]`（读右邻居 5）。
- 如果删掉 `thread::sync_threads();` 这一行，结果会**不稳定/错误**——因为屏障前你就读邻居，邻居可能还没写。

**预期结果**：两次运行都通过校验（程序不 `exit(1)`），证明屏障保证了块内可见性。

> ⚠️ 若本机没有 GPU，可用 `cargo oxide build sharedmem` 只验证能编译通过（编译期共享内存地址分配正确）。运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SharedArray` 用 `PhantomData<UnsafeCell<[T; N]>>` 而不是 `PhantomData<[T; N]>`？

**参考答案**：`UnsafeCell` 自身是 `!Sync` 的，把它放进 `PhantomData` 会让 `SharedArray` 也变成 `!Sync`。共享内存的并发安全依赖硬件屏障，Rust 类型系统看不到，所以类型必须诚实地说「我不能被简单地跨线程共享」。

**练习 2**：如果同一个内核里声明了两个 `SharedArray<f32, 256>`，会不会冲突？

**参考答案**：不会。它们是两个不同的 `static mut`，编译器会为各自分配独立的共享内存区域。sharedmem 示例的 `shared_dual` 就同时用了 `TILE_A` 和 `TILE_B` 两个数组（见示例第 55-79 行）。

---

### 4.2 DynamicSharedArray 动态共享内存

#### 4.2.1 概念说明

`SharedArray` 的大小写死在编译期。但很多场景（CUTLASS 风格的模板内核）希望「同一份 PTX，在不同启动时配不同的共享内存大小」。这就需要**动态共享内存**，对应 CUDA 里的 `extern __shared__`。

cuda-oxide 提供的 `DynamicSharedArray<T, ALIGN>` 不是数组类型，而是一个**入口**：调用 `DynamicSharedArray::<f32>::get()` 拿到一块共享内存的起始指针 `*mut T`，这块内存有多大由宿主启动时通过 `LaunchConfig::shared_mem_bytes` 指定。

#### 4.2.2 核心流程

动态共享内存的工作方式：

```
宿主侧：
  LaunchConfig { shared_mem_bytes: 2048, ... }   ← 在此指定字节数

设备侧：
  let p: *mut f32 = DynamicSharedArray::<f32>::get();   ← 拿到起始指针
  unsafe { *p.add(tid) = ...; }                          ← 用裸指针读写
  thread::sync_threads();
  unsafe { let v = *p.add(other); }
```

几个要点：

- **同一块内存**：一个内核里多次调用 `get()` / `offset(byte)` 都指向**同一块**底层内存。要划分成多个数组，就用 `offset(字节数)` 取不同的起始地址。
- **对齐进类型**：`ALIGN`（默认 16，对齐 nvcc）也是 const generic，编译器据此生成 `.extern .shared .align N` 声明。TMA（张量内存加速器）要求 128 字节对齐。
- **取最大对齐**：当一个内核里混用了 `ALIGN=16/128/256`，编译器在预处理阶段取最大值作为该符号的全局对齐。
- **无越界检查**：因为大小是运行时才知道的，编译期无法检查 `p.add(i)` 是否越界，全靠宿主 `shared_mem_bytes` 给够。

#### 4.2.3 源码精读

**类型与对齐**——又是一个 ZST，默认对齐 16：

[crates/cuda-device/src/shared.rs:375-376](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L375-L376) —— `DynamicSharedArray<T, const ALIGN: usize = 16>`。

**get() / offset()**——都是占位 intrinsic，函数体 `unreachable!()`，由编译器替换：

[crates/cuda-device/src/shared.rs:401-404](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L401-L404) —— `get()` 返回动态共享内存起始处的类型化指针。

[crates/cuda-device/src/shared.rs:450-456](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L450-L456) —— `offset(byte_offset)` 返回偏移若干字节后的指针，用于把同一块内存切成多段。注意 `let _ = byte_offset;` 是为了「使用」参数、防止编译器优化掉它，真正的偏移计算在编译期完成。

**配套查询函数**——`dynamic_smem_size()` 读 `%dynamic_smem_size` 特殊寄存器（仅含动态部分，不含静态 `SharedArray`）：

[crates/cuda-device/src/shared.rs:473-477](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/shared.rs#L473-L477)

**设备侧使用 + 宿主侧配额**——dynamic_smem 示例的 `dynamic_smem_basic` 与它的启动配置，合起来就是动态共享内存的完整契约：

[crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs:47-72](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs#L47-L72) —— 设备侧用 `DynamicSharedArray::<f32>::get()` 拿指针，写入后屏障，再读邻居。

[crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs:247-251](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs#L247-L251) —— 宿主侧把 `shared_mem_bytes` 设成 `N * size_of::<f32>()`，即真正分配的字节数。

**分区用法**——`dynamic_smem_partition` 用 `offset(1024)` 把一块 2048 字节的内存切成两个 256-f32 数组：

[crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs:87-93](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/dynamic_smem/src/main.rs#L87-L93) —— `smem_a = get()`（偏移 0），`smem_b = offset(1024)`（256 个 f32 之后）。

#### 4.2.4 代码实践

**实践目标**：体会「同一份 PTX，不同共享内存大小」的灵活性。

**操作步骤**：

1. 运行示例：`cargo oxide run dynamic_smem`，确认 4 个子测试全部通过。
2. 阅读示例末尾打印的 PTX 符号名（`__dynamic_smem_dynamic_smem_basic` 等），理解每个内核会生成自己专属的 `.extern .shared` 符号。
3. 修改：把 `dynamic_smem_basic` 宿主侧的 `shared_mem_bytes` 故意改小（例如 `N * size_of::<f32>() / 2`），重新运行。

**需要观察的现象**：

- 改小后，线程访问 `*smem.add(tid)` 会越界写到分配范围之外 → 结果错误甚至崩溃。这直观说明动态共享内存**没有编译期越界保护**，全靠宿主给够字节。

**预期结果**：正确配额下 4 个测试通过；配额不足时行为异常。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一个内核里同时写了 `DynamicSharedArray::<f32>::get()`（ALIGN=16）和 `DynamicSharedArray::<f32, 128>::get()`，最终 PTX 里这个共享符号的对齐是多少？

**参考答案**：256 之外的取最大值，这里是 `max(16, 128) = 128`。示例 `dynamic_smem_mixed_align` 演示了 `max(16,128,256)=256` 的情况（见示例第 162-174 行的注释）。

**练习 2**：动态共享内存为什么用裸指针 `*mut T` 而不是像 `SharedArray` 那样实现 `Index`？

**参考答案**：因为大小在编译期未知，无法在 `index()` 里做 `idx < N` 的越界检查；而且动态内存天然需要被切分成多段、用字节偏移寻址，裸指针 + `add()` 才能表达这种布局。代价是失去类型安全，全部访问必须在 `unsafe` 下进行。

---

### 4.3 sync_threads 块级屏障

#### 4.3.1 概念说明

`thread::sync_threads()` 是最基础的同步原语，等价于 CUDA C++ 的 `__syncthreads()`。它的语义是：**块内所有线程都必须到达这个屏障，任何一个线程才能继续往下走**。

要严格区分两个概念，这是初学者最容易混淆的：

- **同步（synchronization）**=「等人」。`sync_threads` 保证所有线程都执行到这一行。它**隐含**了一个内存可见性保证：屏障之前的所有共享内存写，对屏障之后的所有线程可见。
- **栅栏（fence）**=「让数据被看见」，但**不等人**。`threadfence` 只保证「本线程之前的写，对某个作用域内的其他线程可见，且先于本线程之后的写被观察到」，它**不等待**其他线程。

#### 4.3.2 核心流程

`sync_threads` 的硬件实现是 PTX 的 `bar.sync 0`（也叫 `barrier0`）。它的执行模型可以理解成「块内计数器」：

```
所有 N 个线程进入屏障：
  ┌─ 线程到达 → 计数器 +1
  │  当计数器 == N 时，放行所有线程，计数器清零
  └─ 否则该线程在此等待（硬件调度挂起，不占 ALU）
```

由此推出**致命约束**：

> 块内所有线程必须到达**同一个** `sync_threads`，否则死锁。

具体说，**不能把 `sync_threads` 放进只有部分线程会进入的条件分支**。例如下面的写法会死锁：

```rust,ignore
// 错误示范：只有偶数线程进 if，奇数线程永远到不了屏障
if tid % 2 == 0 {
    TILE[tid] = ...;
    thread::sync_threads();   // ← 死锁：奇数线程在屏障外，计数器永远到不了 N
}
```

cuda-oxide 文档在 `sync_threads` 上明确标注了这条安全约束。

#### 4.3.3 源码精读

**设备侧占位函数**——又是 `unreachable!()` 体，注释说明它被降级为 `@llvm.nvvm.barrier0()`：

[crates/cuda-device/src/thread.rs:695-699](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/thread.rs#L695-L699) —— 文档第 690-694 行还强调了「分叉屏障会死锁」。

**编译器侧：MIR → dialect**——mir-importer 在遇到 `sync_threads()` 调用时，生成一个 `Barrier0Op`（dialect-nvvm 提供的操作）：

[crates/mir-importer/src/translator/terminator/intrinsics/sync.rs:46-74](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-importer/src/translator/terminator/intrinsics/sync.rs#L46-L74) —— 注释明确写出它映射到 `nvvm.barrier0` / PTX `bar.sync 0`。

**编译器侧：dialect → LLVM**——mir-lower 再把 `Barrier0Op` 翻译成对 `llvm_nvvm_barrier0` 内联函数的调用：

[crates/mir-lower/src/convert/intrinsics/basic.rs:74-86](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-lower/src/convert/intrinsics/basic.rs#L74-L86) —— `call_intrinsic(ctx, rewriter, op, "llvm_nvvm_barrier0", ...)`。

把这两段串起来，你就看到了 cuda-oxide 处理 intrinsic 的标准两步：**先在 mir-importer 把 Rust 函数调用识别成方言操作（`Barrier0Op`），再在 mir-lower 把方言操作降级成 LLVM/PTX 原语（`llvm_nvvm_barrier0`）**。

#### 4.3.4 代码实践

**实践目标**：用块内求和验证 `sync_threads` 的协作语义（这正是本讲规格指定的练习任务）。

**操作步骤**：在某个示例的 `#[cuda_module] mod kernels` 里新增如下内核（示例代码，非项目原有代码）：

```rust,ignore
#[kernel]
pub fn block_sum(out: DisjointSlice<u32>) {
    static mut TILE: SharedArray<u32, 256> = SharedArray::UNINIT;

    let tid = thread::threadIdx_x() as usize;

    // 1. 每个线程把自己的线程号写进共享内存
    unsafe { TILE[tid] = tid as u32; }

    // 2. 等所有线程写完
    thread::sync_threads();

    // 3. 线程 0 顺序求和，写回全局内存
    if tid == 0 {
        let mut sum: u32 = 0;
        for i in 0..256 {
            unsafe { sum += TILE[i]; }
        }
        unsafe { *out.get_unchecked_mut(0) = sum; }
    }
}
```

用 1 个块、256 个线程启动它。

**需要观察的现象 / 预期结果**：线程 0 写回的应是 `0+1+2+…+255`。用求和公式

\[
S = \sum_{i=0}^{B-1} i = \frac{B(B-1)}{2}
\]

代入 \(B=256\)，得 \(S = 256 \times 255 / 2 = 32640\)。若输出是 32640，说明屏障正确保证了「线程 0 读到了所有线程的写入」。

**思考实验**（不必运行）：如果删掉 `sync_threads()`，线程 0 的循环可能读到尚未被其它线程写入的槽位（读到未初始化值），结果会偏小且不确定。

> 运行结果「待本地验证」（需要块大小恰好为 256 的 GPU 内核执行环境）。

#### 4.3.5 小练习与答案

**练习 1**：`sync_threads()` 放在 `if tid < 128 { ... }` 分支里会怎样？

**参考答案**：死锁。块内 256 个线程只有 128 个能到达屏障，计数器永远凑不齐 256，到达的线程永远挂起，内核挂死。规则：`sync_threads` 必须被块内**所有**线程无条件到达。

**练习 2**：`sync_threads()` 能不能跨块同步？

**参考答案**：不能。它的作用域是单个线程块（CTA）。跨块同步需要协作组（cooperative groups）的网格级同步，或用全局内存 + 原子操作自己搭。这超出本讲范围。

---

### 4.4 threadfence 系列内存栅栏

#### 4.4.1 概念说明

`threadfence` 解决的是另一个问题：**可见性与顺序**，而不是「等人」。

典型场景是「生产者-消费者」模式：线程 A 往全局内存写数据，再写一个「就绪标志」；线程 B 轮询标志，看到就绪后去读数据。问题是，硬件和编译器都可能**重排**内存写——如果没有栅栏，线程 B 可能看到「标志已就绪」但「数据还没写进去」。

`threadfence` 在「写数据」和「写标志」之间插一道栅栏，保证：本线程在栅栏之前的所有写，对作用域内的其它线程可见，且先于栅栏之后的写被观察到。**它不阻塞、不等待别的线程。**

cuda-oxide 提供三档作用域，对应 PTX 的三档 `membar`：

| cuda-oxide 函数 | PTX 指令 | 作用域 | 适用场景 |
|-----------------|----------|--------|----------|
| `threadfence_block()` | `membar.cta` | 同一个块内 | 块内协作（配合共享/全局内存） |
| `threadfence()` | `membar.gl` | 整个 GPU 设备 | 跨块、同 GPU 上的发布-消费 |
| `threadfence_system()` | `membar.sys` | 整个系统（GPU+CPU+其它 GPU） | 跨 GPU、GPU-CPU 的标志发布 |

作用域越大，开销越大。能用 `.cta` 就别用 `.gl`，能用 `.gl` 就别用 `.sys`。

#### 4.4.2 核心流程

发布-消费的标准写法：

```
生产者线程：
  data[i] = value;          // 1. 写数据
  thread::threadfence();    // 2. 设备级栅栏：让上面的写对全 GPU 可见
  flag[i] = READY;          // 3. 写就绪标志（通常用原子）

消费者线程：
  while (atomic_load(flag[i]) != READY) {}   // 轮询标志
  // 此时 data[i] 一定已经可见
  let v = data[i];
```

关键直觉：栅栏是**单向的顺序保证**，它约束的是「同一个线程自己前后写的顺序在别的线程看来是怎样的」，而不是「两个线程在时间上汇合」。后者是 `sync_threads` 干的事。

注意 `threadfence` 与 `sync_threads` 的关系：`sync_threads` 隐含了块级可见性（等价于一次 `membar.cta` + 一次汇合），所以在「纯块内 + 共享内存」的协作里，`sync_threads` 通常就够了，不必额外加 `threadfence_block`。`threadfence` 系列主要用于**全局内存**的跨线程/跨块可见性。

#### 4.4.3 源码精读

**设备侧三个占位函数**——fence.rs 的全部内容，注释直接给出对应的 PTX 指令：

[crates/cuda-device/src/fence.rs:24-28](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/fence.rs#L24-L28) —— `threadfence_block` → `membar.cta`。

[crates/cuda-device/src/fence.rs:36-40](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/fence.rs#L36-L40) —— `threadfence` → `membar.gl`。

[crates/cuda-device/src/fence.rs:50-54](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/fence.rs#L50-L54) —— `threadfence_system` → `membar.sys`，文档还点明它配合 `cuda_device::atomic::SystemAtomicU32` 发布跨 GPU 的就绪标志。

**编译器侧：栅栏降级为内联 PTX 汇编**——mir-lower 的 `convert_membar` 把方言操作翻译成一段带 `~{memory}`（编译器内存屏障 clobber）的内联汇编：

[crates/mir-lower/src/convert/intrinsics/basic.rs:88-105](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-lower/src/convert/intrinsics/basic.rs#L88-L105) —— 三个 `convert_threadfence_*` 函数都复用 `convert_membar`，只是传入不同的 `membar.cta;` / `membar.gl;` / `membar.sys;` 模板：

[crates/mir-lower/src/convert/intrinsics/basic.rs:107-135](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/mir-lower/src/convert/intrinsics/basic.rs#L107-L135)。

对比 4.3 节：`sync_threads` 走的是 LLVM 内联函数 `llvm_nvvm_barrier0`，而 `threadfence` 走的是 PTX 内联汇编 `membar.*`——这正好对应「汇合」与「顺序保证」两类不同的硬件机制。

#### 4.4.4 代码实践

**实践目标**：通过阅读测试断言，理解 `threadfence` 在「标志发布」里的正确位置。

**操作步骤**（源码阅读型实践，因为正确实现一个跨块发布-消费内核需要原子操作，超出本讲范围）：

1. 在仓库里搜索 `threadfence` 的使用点：
   ```bash
   # 在 examples 与 crates 中搜索 threadfence 的真实调用
   ```
2. 阅读 [crates/rustc-codegen-cuda/examples/atomics/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/examples/atomics/src/main.rs) 中是否包含「写数据 → threadfence → 写原子标志」的模式，找到 fence 调用的位置。
3. 回答：在这个例子里，为什么 fence 用的是 `threadfence()`（`.gl`）而不是 `threadfence_block()`（`.cta`）？

**需要观察的现象 / 预期结果**：你应该能解释——因为消费方可能是**别的线程块**（标志放在全局内存里被任意块轮询），块级 `.cta` 栅栏只对块内可见，无法保证跨块可见性，所以必须用设备级 `.gl`。这正是 4.4.1 表格里「作用域」一列的实战含义。

#### 4.4.5 小练习与答案

**练习 1**：用一句话区分 `sync_threads()` 和 `threadfence()`。

**参考答案**：`sync_threads()` 是「等块内所有线程汇合」（隐含块级可见性，且会阻塞）；`threadfence()` 是「让本线程之前的写在指定作用域可见、且不被重排到后面的写之后」（不阻塞、不等人）。

**练习 2**：跨 GPU 发布一个就绪 `SystemAtomicU32` 标志前，应该用哪一档 fence？为什么？

**参考答案**：`threadfence_system()`（`membar.sys`）。因为消费方在另一块 GPU 甚至 CPU 上，只有系统级作用域能保证写可见性；`.cta`（块内）和 `.gl`（单设备）都不够。

---

## 5. 综合实践

把本讲四个最小模块串起来，设计一个**块内归约（block reduction）**小任务：求一个长度等于块大小（256）的向量在该块内的总和。

要求：

1. 用 **`SharedArray<f32, 256>`** 作为块内暂存区。
2. 每个线程从全局内存读一个元素，写入共享内存下标 `tid`。
3. 调用 **`sync_threads()`** 保证全部就位。
4. 用「树形归约」：每轮活跃线程数减半，前一半线程把后一半的值累加到自己槽位，每轮之间都用 `sync_threads()` 隔开。最终线程 0 持有总和。
5. 线程 0 把结果写回全局内存 `out[0]`。

树形归约的迭代次数与每轮线程数：

\[
\text{轮数} = \lceil \log_2 B \rceil, \qquad
\text{第 } k \text{ 轮步长 } = 2^{k}
\]

对 \(B=256\)，共需 8 轮。

**进阶追问**（选做）：

- 如果把这个归约的输入改成跨多个块（grid > 1），每个块各自归约出一个部分和，再要得到全局总和，你会把 `threadfence()` 放在哪里、配合什么原子操作？（提示：每块的线程 0 用 `atomicAdd` 把部分和累加到一个全局计数器，并在累加前用 `threadfence()` 保证部分和已对其它块可见。这部分依赖本讲未展开的原子 API，可在 [u2 能力矩阵](u1-l5-examples-tour.md) 里找到 `atomics` 示例后再实现。）

**验收标准**：单块（grid=1, block=256）下，`out[0]` 等于输入向量元素之和；若删掉任意一轮之间的 `sync_threads()`，结果会出错。运行结果「待本地验证」。

## 6. 本讲小结

- **共享内存**是块内协作的快黑板：块内共享、块间隔离、内核结束失效。`SharedArray<T, N>` 是其静态、编译期定大小的封装，编译器把它放进 LLVM address space 3。
- **使用范式**是「写共享内存 → `sync_threads()` → 读别人的值」，所有访问都在 `unsafe` 下，因为内存未初始化且并发访问。
- **`DynamicSharedArray<T, ALIGN>`** 把共享内存大小推迟到启动时的 `LaunchConfig::shared_mem_bytes`，用裸指针 + `offset()` 表达，支持同一份 PTX 配不同 smem 大小，代价是无编译期越界检查。
- **`sync_threads()`** 是块级汇合屏障（`bar.sync 0`），必须被块内所有线程无条件到达，否则死锁；它隐含块级内存可见性。
- **`threadfence` 系列**是「让写在指定作用域可见」的顺序保证（`membar.cta/gl/sys`），不阻塞、不等人，三档作用域对应块/设备/系统。
- cuda-oxide 的同步/栅栏 intrinsic 都是「函数体为 `unreachable!()` 的占位符」，由 mir-importer 识别成方言操作、再由 mir-lower 降级为 LLVM 内联函数或 PTX 内联汇编。

## 7. 下一步学习建议

- **向「warp 级编程」进阶**：共享内存 + `sync_threads` 是块级协作；更细粒度的是 warp（32 线程）内的协作与归约，见 `crates/cuda-device/src/warp.rs` 以及 `examples/warp_reduce`。
- **向「Hopper 异步屏障」进阶**：本讲的 `sync_threads` 是同步屏障（线程要等着）；现代架构有 `mbarrier` 异步屏障，能让 TMA 异步拷贝「拷完自动通知」，线程不必空等。源码在 [crates/cuda-device/src/barrier.rs](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-device/src/barrier.rs)，编译器侧在 `crates/mir-importer/src/translator/terminator/intrinsics/sync.rs`（本讲引用过的同一文件，后半部分全是 `mbarrier_*`）。
- **把归约做对做快**：尝试实现综合实践里的树形归约，然后对照 `examples/` 下与归约/扫描相关的示例，检验你的实现。
- **理解编译器如何识别这些 intrinsic**：复习本讲引用的 `mir-importer/.../sync.rs` 与 `mir-lower/.../basic.rs`，它们是后续 [u4 编译流水线总览](u4-l1-backend-entry-and-split.md) 与 [u6 编译器深潜](u2-l1-kernel-and-cuda-module-macros.md) 单元的实物入口。
