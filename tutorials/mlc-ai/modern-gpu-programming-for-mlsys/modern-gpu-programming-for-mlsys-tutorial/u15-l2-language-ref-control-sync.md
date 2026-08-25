# TIRx 语言参考：控制流与线程同步

## 1. 本讲目标

本讲是 TIRx 语言参考手册型讲义的第二篇（第一篇 u15-l1 覆盖解析工具、数据类型与 buffer），系统梳理语言参考的后两块内容：**控制流**与**线程/同步原语**。学完后你应该能够：

1. 说出 TIRx 的 `if`、四种循环、`while` 各自映射到什么 CUDA 控制流，以及表达式级选择 `T.if_then_else` 的定位。
2. 解释「均匀（uniform）控制流」与「发散（divergent）控制流」的区别，判断一条集体操作能否放进某个 `if` 分支。
3. 查阅 `T.cuda.*` / `T.ptx.*` 两个内置函数命名空间，找到同步、mbarrier、reduction 与 PTX 数据搬运等各类 intrinsic。
4. 区分 `cta_sync`、`warp_sync`、`warpgroup_sync(id)`（命名屏障）与 mbarrier 的**协作范围**，并掌握 elect 选举、fence 排序、mbarrier 相位这三个最容易出错的语义。
5. 拿着本讲的清单，对 `hgemm_v1` 与 GEMM Step 7 两个真实内核逐条标注每个 `if` 守卫与每条同步语句的语义与协作范围——这是本讲的综合实践。

## 2. 前置知识

本讲假设你已读过以下讲义（或具备等价认知），涉及的概念只简要回顾：

- **u2-l1 线程执行层级**：GPU 有 thread、warp（32 线程 SIMT 锁步）、warpgroup（4 warp / 128 线程）、CTA、cluster、grid 六级。warp 内分支发散时，两条路径会被串行执行。「协作范围」这个词贯穿本讲，指的就是某条语句需要哪个层级的多少线程一起到达或一起执行。
- **u8-l1 / u8-l2 mbarrier**：mbarrier 是共享内存中的硬件同步对象，内部维护相位（phase）、到达计数与在途字节计数；`try_wait` 只观察、不修改状态；屏障复用必须翻转本地相位变量。
- **u9-l1 / u11-l2 hgemm_v1**：单 tile、单 CTA、128 线程（1 个 warpgroup、4 个 warp）的第一个 TIRx 内核，加载由全 CTA 协作、MMA 由单线程发起。
- **u13-l1 Step 7（warp 特化）**：单 warpgroup 串行控制流被拆成三个并发角色（TMA producer warp、MMA consumer warp、回写 warpgroup），用 `if wg_id == ...` 守卫划分。
- **u15-l1 语言参考（上）**：`T.meta_var`、`@T.inline` 等解析期工具；「普通 `range` 是串行循环、`T.unroll` 才会展开」这一点 u15-l1 已提过，本讲 4.1 会补全四种循环的完整清单。

一个贯穿本讲的直觉：**控制流语法决定「谁执行哪段代码」，同步原语决定「谁等谁」**。前者管分支与循环的划分，后者管线程组之间以及线程与异步引擎（TMA、Tensor Core）之间的交接。TIRx 不隐藏硬件（u9-l1），所以这两块在 TIRx 里都是显式的、需要程序员自己写对的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tirx_guide/language_reference/cuda/control_flow.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst) | 语言参考「控制流」一节：if、均匀 vs 发散、四种循环、while |
| [tirx_guide/language_reference/cuda/threads_sync.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst) | 语言参考「CUDA C++/PTX intrinsics」一节：`T.cuda.*`/`T.ptx.*`、四个高频同步语义、内联 CUDA |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | hgemm_v1 内核全文（L84–L170），本讲实践的标注对象一 |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | GEMM Step 7 内核（L151–L302）与 `warpgroup_sync` 说明（L91–L105），本讲实践的标注对象二 |

阅读建议：先精读两份 rst（合计不到 300 行），再带着清单回到两个内核，你会发现书中内核里的每一行守卫与同步都能在参考中找到出处——这正是「手册型讲义」的用法：不是背下来，而是建立「遇到某类语句→去参考哪一节查」的索引。

## 4. 核心概念与源码讲解

### 4.1 控制流：if、四种循环与 while

#### 4.1.1 概念说明

TIRx 的控制流刻意保持「薄」：它不发明新的循环语义、不做自动并行化变换，Python 写什么就直译成什么 CUDA 控制流。这与 TIRx 「不隐藏硬件」的定位一致——并行不是编译器从循环里帮你找出来的，而是你用 tile 操作与线程层级显式声明的（u9-l1 的三要素）。

因此 TIRx 控制流要回答的问题只有一个：**这段代码由哪些线程执行、执行几遍**。它提供三组工具：

- `if` / `else`：分支，守卫条件通常是线程 ID 比较；
- 四种循环：`T.serial`、`T.unroll`、`T.vectorized`、`T.grid`；
- `while`：条件循环，配可变标量计数器。

另有一个不产生控制流分支的表达式级选择 `T.if_then_else`，用于「只想选个值、不想分叉执行路径」的场合。

#### 4.1.2 核心流程

一个 TIRx 控制流语句到 CUDA 的映射过程可以概括为：

1. 解析期：Python `if`/`for`/`while` 被 TIRx 源码检视机制捕获，转成 TIR 的 `IfThenElse`、循环体节点（u15-l1 讲过 TIRx 靠 Python 源码检视解析内核，所以这些语句必须写在文件或 notebook 单元格里）。
2. Lowering 期：循环注解（`T.serial` 等）决定该循环在 TIR 中的类型——串行、展开、向量化或循环嵌套。
3. 代码生成：每种节点映射到对应的 CUDA 语法——`if` 到 `if`，`T.serial` 到 `for`，`T.unroll(4)` 到四条直线语句，`while` 到 `while(1)` 加提前 `break`。

四种循环的分工：

| 循环 | 语义 | 典型用途 |
| --- | --- | --- |
| `T.serial(n)` | 顺序循环（ptxas 仍可能展开它） | K 维累加、tile 迭代；普通 `range` 就是它 |
| `T.unroll(n)` | 完全展开成直线语句 | 编译期已知小次数、想要寄存器分配机会时 |
| `T.vectorized(n)` | 向量化循环 | 让相邻迭代合并成向量访存 |
| `T.grid(*extents)` | 循环嵌套 | 一次声明多维迭代空间 |

#### 4.1.3 源码精读

**（1）if 的两种守卫写法。** 参考明确给出：Python `if`/`else` 直接变成 CUDA `if`/`else`，守卫方式一是「线程/lane 比较」，二是用 `T.ptx.elect_sync()` 选举唯一发起线程，见 [tirx_guide/language_reference/cuda/control_flow.rst:L24-L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L24-L39)。这段同时给出了两段对照代码：`if tx < 128:` 的分支两侧各改写 `A[tx]`，以及 `if T.ptx.elect_sync():` 里放「由一个被选中的 lane 发起 TMA/MMA」类操作——后者正是全书内核发起异步 tile 操作的标准姿势。

**（2）表达式级选择。** `T.if_then_else(cond, a, b)` 不引入 TIRx 控制流分支，lowering 成三元表达式，最终由后端决定用什么机器指令实现这个表达式，见 [tirx_guide/language_reference/cuda/control_flow.rst:L49-L55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L49-L55)。生成的 CUDA 就是 `O_ptr[tx] = (A_ptr[tx] > 0.0f) ? A_ptr[tx] : 0.0f;` 一行。它和 `if` 的区别在于：`if` 分叉的是「执行哪些语句」，`T.if_then_else` 只选「一个值」。

**（3）四种循环与 grid 例子。** [tirx_guide/language_reference/cuda/control_flow.rst:L77-L100](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L77-L100) 列出四种循环并说明 `break`/`continue` 可用。其中 `for i, j in T.grid(8, 8):` 一行声明二维迭代空间，直译成两层 `for`。

**（4）while 的 lowering。** [tirx_guide/language_reference/cuda/control_flow.rst:L102-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L102-L126) 给出完整对照：TIRx 里 `i: T.int32 = 0` 声明可变标量，`while i < 64:` 循环体中 `i += 1`；生成的 CUDA 把计数器放进单元素寄存器缓冲 `i_ptr[1]`，循环写成 `while (1) { if (!(i_ptr[0] < 64)) { break; } ... }`。可变标量的载体（寄存器 buffer）属于 u15-l1 的 buffer 知识，这里只需记住：**while 的条件变量是一个真实的每线程寄存器槽位，每线程各自维护一份**。

**（5）真实内核中的循环选型。** hgemm_v1 本身没有循环（它是单 tile 无 K 循环版本）；到了 Step 2，K 累加写成 `T.serial(K_TILES)` 循环（u11-l3）。Step 7 中可以看到三类循环并存：持久调度外层 `while tile_scheduler.valid():`（while 型）、K 循环 `for k in range(K_TILES):`（即 `T.serial`）、以及 barrier 初始化等无循环直线段，见 [chapter_gemm_advanced/index.md:L221-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L221-L229)——生产者角色的 `while` + `for range` 嵌套。注意这个 `while` 的条件 `tile_scheduler.valid()` 是每线程各自求值的标量，同一 warp 内所有 lane 对它求值结果一致，所以循环本身不会发散。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Python 控制流 → CUDA 控制流」的直译关系，把参考中的三段例子合成一个内核并检视两级代码。

**操作步骤**（源码阅读 + 本地构造型，无 GPU 也可完成前两步）：

1. 新建文件 `ctrl_flow_probe.py`，把参考中的三段例子合并成一个 `@T.prim_func`（示例代码，非项目原有代码，骨架参照 [chapter_intro_tirx/index.md:L95-L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L95-L104) 的头两行写法）：

   ```python
   # 示例代码：合并 control_flow.rst 的 if / T.grid / while 三个例子
   @T.prim_func
   def probe(A_ptr: T.handle, B_ptr: T.handle):
       A = T.match_buffer(A_ptr, (64,), "float32")
       B = T.match_buffer(B_ptr, (64,), "float32")
       T.device_entry()
       tx = T.thread_id([64])
       # 例 1：if/else（对照 control_flow.rst L33-L36）
       if tx < 32:
           A[tx] = A[tx] * T.float32(2.0)
       else:
           A[tx] = A[tx] + T.float32(1.0)
       # 例 2：T.grid（对照 control_flow.rst L91-L92）
       for i, j in T.grid(8, 8):
           B[i * 8 + j] = T.max(A[i * 8 + j], T.float32(0.0))
   ```

   注意：TIRx 靠 Python 源码检视解析内核，代码必须写在文件里，不能塞进 `python -c`（u1-l3、u9-l2 反复强调的纪律）。
2. 无 GPU 环境：在文件末尾加 `probe.show()`，运行 `python ctrl_flow_probe.py`，观察 tile 级 IR 中的 `if`、`T.grid` 节点是否与源码一一对应。
3. 有 GPU 环境：按 u9-l2 的编译回路 `tvm.compile(tvm.IRModule({"main": probe}), target="cuda", tir_pipeline="tirx")` 编译，再用 `ex.mod.imports[0].inspect_source()` 打开生成的 CUDA 源，逐行对照：`if (((int)threadIdx.x) < 32)`、两层 `for`、（若加了 while 例）`while (1) { if (!(...)) { break; } }`。

**需要观察的现象**：IR 层的分支与循环结构同 Python 源码形状完全一致；CUDA 源中没有出现任何编译器自行生成的额外分支（守卫不会自动加）。

**预期结果**：三段控制流在 CUDA 中都能找到逐句对应物；特别确认 `if tx < 32` 之外**没有**自动包裹任何线程守卫——守卫必须自己写，这是 4.2 的伏笔。

**待本地验证**：本实践的具体输出文本（IR 与 CUDA 源的精确格式随 TVM 版本变化，以本地 `apache-tvm==0.26.0` 实际打印为准）。

#### 4.1.5 小练习与答案

**练习 1**：`T.if_then_else(cond, a, b)` 和 `if cond: ... else: ...` 都能「按条件二选一」，什么时候必须用前者？

**参考答案**：当只需要**选一个值**而不想分叉执行路径时用 `T.if_then_else`——它 lowering 成三元表达式，出现在赋值右侧，不产生控制流分支。当代码要根据条件执行**不同的语句序列**（例如一边发起 TMA、一边做别的）时用 `if/else`。另外注意 `if` 分支会参与「均匀性」判断（见 4.2），而表达式选择不涉及集体操作到达问题。

**练习 2**：Step 7 的 TMA 生产者角色里 `for k in range(K_TILES):` 是四种循环里的哪一种？它和 `T.unroll` 的区别是什么？

**参考答案**：普通 `range` 成为 `T.serial`——顺序循环，ptxas 仍可能在后端把它展开。`T.unroll` 则在 TIR 层就完全展开成直线语句，循环消失。`T.serial` 保留循环结构（K_TILES 是运行期才知道时只能这样写），`T.unroll` 适合编译期已知的小次数且希望每条语句独立参与寄存器分配的场景。

**练习 3**：`while` 的条件变量在生成的 CUDA 里放在哪里？这意味着 128 个线程各自执行同一个 `while` 时，循环变量的值会互相干扰吗？

**参考答案**：放在一个单元素寄存器缓冲 `i_ptr[1]` 里（对照 control_flow.rst 的 lowering 例子）。不会干扰——寄存器是每线程私有的（u2-l2），每个线程维护自己的一份计数器，各自独立推进同一个循环形状。

### 4.2 均匀与发散：集体操作的控制流纪律

#### 4.2.1 概念说明

控制流语法本身很简单，真正决定内核死活的是**均匀性（uniformity）**：一条**集体操作**（collective operation）——如 CTA 级屏障、warpgroup 集体 tile 操作——必须被它同步的**每一个线程一致地到达**。如果有的线程进了这个分支、有的没进，到达数凑不齐，屏障永远无法完成，内核死锁。

术语解释：

- **发散（divergent）控制流**：同一层级的线程对守卫条件求值结果不一致，各走各的分支。warp 内发散导致两条路径串行执行（u2-l1）；更糟的是 warpgroup / CTA 级发散会让集体操作缺员。
- **均匀（uniform）控制流**：参与某集体操作的所有线程对守卫求值一致，集体操作全员到达。
- **守卫（guard）**：用 `if` 把一段代码限定给一部分线程执行的写法，例如 `if warp_id == 0:`。

判断口诀：**先问这条语句的协作范围是多大，再问守卫放进来的线程是不是恰好就是这个范围**。

#### 4.2.2 核心流程

拿到一段 TIRx 内核，按三步检查均匀性：

1. **标出所有集体操作**：`cta_sync()`、`warpgroup_sync(id)`、`Tx.wg.*`（warpgroup 集体）、`Tx.cta.*`（CTA 协作）、mbarrier 的 arrive/wait（到达数必须与初始化一致）。
2. **沿控制流向外找守卫**：这条集体操作处于哪些 `if` 分支的内部？把每个守卫允许通过的线程集合算出来（`wg_id == 0` 是 128 线程，`warp_id == 0` 是 32 线程，`elect_sync` 是 1 线程）。
3. **对比两个集合**：守卫允许的线程集合 ⊇ 且 ≈ 集体操作要求的集合 → 安全；守卫选出的线程少于集体操作要求 → 死锁；多余线程也执行 → 到达数超预期，mbarrier 行为错乱。

特别注意参考给出的屏障初始化陷阱：高层包装 `MBarrier.init()` 自带单线程守卫（生成 `if (threadIdx.x < 1)`），把它嵌进另一个发散分支可能让屏障根本没被初始化，造成未定义的 launch 失败；而原始 `T.ptx.mbarrier.init` intrinsic **不会**自动加守卫，调用者必须自己选出初始化线程。

#### 4.2.3 源码精读

**（1）参考中的死锁警告。** [tirx_guide/language_reference/cuda/control_flow.rst:L57-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L57-L69) 明确写道：`T.cuda.cta_sync()` 映射 `__syncthreads()`，要求线程块内**全部**线程到达；绝不能放进线程级或 warpgroup 级发散分支——如果放在 `if wg_id == 0:` 里，其他 warpgroup 永远到不了，内核死锁。只需同步一个 warpgroup 时，用 warpgroup 作用域的 `T.cuda.warpgroup_sync(id)`。

**（2）屏障初始化的守卫陷阱。** [tirx_guide/language_reference/cuda/control_flow.rst:L71-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/control_flow.rst#L71-L75)：高层 `MBarrier.init()` 包装会发出单线程守卫，嵌套在发散分支内可能让屏障未初始化并引发未指定的 launch 失败；原始 `T.ptx.mbarrier.init` 不自动加守卫。

**（3）hgemm_v1：手写守卫调原始 intrinsic。** hgemm_v1 用的是原始 `T.ptx.mbarrier.init`，所以守卫自己写，见 [chapter_intro_tirx/index.md:L118-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L118-L126)：`if warp_id == 0:` 选出 0 号 warp（32 线程），内层 `if lane_id == 0:` 再收敛到恰好 1 个线程执行 `T.ptx.mbarrier.init`；同分支内（warp 0 全体）执行 `T.ptx.tcgen05.alloc`——因为 TMEM 分配是 warp 集体指令（u7-l3）。守卫后的三行 fence 与 `cta_sync()` 回到**顶层**（无守卫），全体 128 线程到达：`cta_sync` 在这里同时保证屏障初始化和 TMEM 基地址对全体线程可见。

**（4）Step 7：高层包装免守卫 + 顶层的两道安全线。** Step 7 改用高层包装初始化四道屏障：`tma2mma.init(1)`、`mma2tma.init(1)`、`mma2ld.init(1)`、`ld2mma.init(128)`，见 [chapter_gemm_advanced/index.md:L175-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L175-L180)——注意这里**没有任何手写守卫**，正对应参考说的「包装自带单线程守卫」。随后 TMEM 分配才用 `if wg_id == 0:` / `if warp_id == 0:` 双层守卫（warp 集体指令只需一个 warp 执行），然后 fence 两连发 + `cta_sync()` 在顶层让全部 256 线程（2 个 warpgroup）汇合，见 [chapter_gemm_advanced/index.md:L182-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L182-L188)。

**（5）Step 7：角色分支内为什么禁止 cta_sync。** 三个角色用 `if wg_id == 1:`（内再分 warp 3 / warp 0）与 `elif wg_id == 0:` 划分，见 [chapter_gemm_advanced/index.md:L204-L259](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L204-L259)。回写角色（WG0 全体 128 线程）需要在写完 `Dsmem` 后等整 tile 写齐，但它**不能**用 `cta_sync()`——WG1 的两个 warp 正在跑各自的持久循环，永远到不了这道屏障。书中正文专门解释了这一点并给出替代：warpgroup 作用域的命名屏障 `warpgroup_sync(10)`，见 [chapter_gemm_advanced/index.md:L91-L103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L91-L103)。唯一一道角色分支外的 `cta_sync()` 在清理段（所有角色循环都已退出），见 [chapter_gemm_advanced/index.md:L296-L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L296-L297)——此时全员到达，安全。

#### 4.2.4 代码实践

**实践目标**：建立「守卫集合 vs 集体操作要求集合」的肌肉记忆，用两个思想实验检验理解。

**操作步骤**：

1. 打开 [chapter_gemm_advanced/index.md:L259-L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L259-L294)（Step 7 回写分支），数一数这段代码里的同步语句：`mma2ld.wait`、`tcgen05.fence.after_thread_sync`、`tcgen05.wait.ld`、`ld2mma.arrive(0)`、`fence.proxy_async`、两道 `warpgroup_sync(10)`。
2. 思想实验 A：把第一道 `T.cuda.warpgroup_sync(10)`（L285）改成 `T.cuda.cta_sync()`，推演内核行为。
3. 思想实验 B：把清理段的 `T.cuda.cta_sync()`（L297）移到 `elif wg_id == 0:` 分支的末尾（L294 `tile_scheduler.next_tile()` 之后），推演内核行为。
4. 对照本节（2）（3）（5）三处源码说明核对你的推演。

**需要观察的现象**（纯推演，无需 GPU）：两个实验里哪一类线程会永远等待、等待发生在哪条语句上。

**预期结果**：实验 A——回写分支只有 WG0 的 128 线程执行，`cta_sync()` 需要 256 线程全部到达，WG1 的 64 个活跃线程（warp 3 与 warp 0 各 32 线程，其余 warp 被 `if warp_id` 守卫排除后在分支外空转）永远不来，WG0 卡死在 `__syncthreads()`，内核死锁。实验 B——同理，且更隐蔽：清理段的 `cta_sync` 本来是让全员在退出前汇合的最后一道闸，移进 WG0 分支后 WG1 永远等不到，同时清理段的 TMEM 释放代码对 WG1 也会提前/滞后执行，行为未定义。

**待本地验证**：以上为源码推演结论；若在 Blackwell GPU 上实际运行改后的内核，预期表现为内核不返回（挂起）并最终被 CUDA 驱动或超时机制终止，具体报错形式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`if tx < 128:` 这样的每线程守卫有问题吗？参考怎么说。

**参考答案**：没有问题——参考原文说「per-thread guards such as `if tx < 128` are fine for ordinary work」，每线程守卫用于普通工作负载划分是正常写法（如 hgemm_v1 回写段每线程写自己的一行）。出问题的唯一情形是**集体操作**被放进了线程级或 warpgroup 级发散分支，使需要同步的线程无法全部到达。

**练习 2**：Step 7 用 `tma2mma.init(1)` 等高层包装初始化屏障时没写守卫，hgemm_v1 用 `T.ptx.mbarrier.init` 时写了 `if warp_id == 0: if lane_id == 0:`。为什么？

**参考答案**：高层 `MBarrier.init()` 包装自动发出单线程守卫（`if (threadIdx.x < 1)`），所以顶层裸调是安全的（但把它再嵌进发散分支反而可能让守卫落空、屏障未初始化）；原始 `T.ptx.mbarrier.init` intrinsic 不加守卫，初始化线程必须由调用者显式选出——hgemm_v1 因此手写了两层守卫收敛到 lane 0。

**练习 3**：判断以下语句放在 `if warp_id == 0:` 内是否安全（warpgroup 有 4 个 warp）：(a) `T.cuda.warp_sync()`；(b) `Tx.wg.copy_async`；(c) `T.cuda.cta_sync()`。

**参考答案**：(a) 安全——`warp_sync` 是 `__syncwarp`，只需本 warp 32 线程到达，warp 内 `warp_id` 是常量、不发散。(b) 危险——`Tx.wg.*` 是 warpgroup 集体操作，需要 4 个 warp（128 线程）一起执行，只放进 warp 0 会让集体操作缺员。(c) 危险——`cta_sync` 需要整个 CTA 到达，只让 warp 0 到达会死锁（除非整个内核只有这一个 warp，但那时 warpgroup 概念也不完整）。

### 4.3 后端内置函数：T.cuda.* / T.ptx.* 与内联 CUDA

#### 4.3.1 概念说明

当没有 tile primitive 覆盖所需操作时（u9-l1 讲过 tile 操作与底层辅助的分层），TIRx 提供两条更底层的退路，这就是 threads_sync.rst 开篇的话：

1. **调用后端 intrinsic**：`T.cuda.*` 与 `T.ptx.*` 两个命名空间，直接暴露 CUDA 后端的设备 intrinsic——同步、mbarrier、reduction，以及 PTX 的数据搬运 / MMA 家族。
2. **内联原始 CUDA**：连 intrinsic 都没有的操作，用 `T.cuda.func_call` 把一段 `__device__` 函数源码原样注入。

把这个三层调用栈与 u9-l1 的结论对齐：`Tx.*` 是 tile 操作（声明做什么），`T.ptx.*` / `T.cuda.*` 是底层辅助（资源与同步），再往下是内联 CUDA 兜底。本讲关注中间层，因为**所有同步原语都住在这一层**。

#### 4.3.2 核心流程

使用后端 intrinsic 的流程：

1. 从 `tvm.backend.cuda` 的 API 参考中找到目标函数（threads_sync.rst 提到的家族包括 `cp_async`（LDGSTS）、`cp_async.bulk.tensor`（TMA）、`ldmatrix`/`stmatrix`、`tcgen05.*`（Blackwell MMA）、`atomic_add`、`fence` 等）。
2. 在内核中以 `T.cuda.名字(...)` 或 `T.ptx.名字(...)` 调用，参数是 buffer 指针 / 立即数。
3. lowering 时每个 intrinsic 直接映射到一条 CUDA 内建函数或 PTX 指令，不再有展开逻辑（这与 tile 操作不同——`Tx.gemm_async` 会被展开成 4 条 `tcgen05.mma`，u9-l1）。

#### 4.3.3 源码精读

**（1）intrinsic 速览。** [tirx_guide/language_reference/cuda/threads_sync.rst:L31-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L31-L40) 列出最常用的一组：`T.cuda.cta_sync()`（块屏障，即 `__syncthreads`）、`T.cuda.warp_sync()`（`__syncwarp`）、`T.cuda.warpgroup_sync(8)`（warpgroup 屏障）、`T.cuda.cta_sum(val, num_warps, scratch.ptr_to([0]))`（块级归约），以及 mbarrier 三件套 `T.ptx.mbarrier.init` / `try_wait`——注意这里 mbarrier 的初始化示例配了 `T.alloc_shared((1,), "uint64")` 分配屏障槽位，这正是 hgemm_v1 中 `mma_bar = pool.alloc((1,), "uint64", align=8)` 的对应物。

**（2）可运行的 warp 归约例子。** [tirx_guide/language_reference/cuda/threads_sync.rst:L42-L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L42-L63) 给出全书参考中唯一一个完整可运行的 intrinsic 内核 `warp_reduce`：每 lane 先装入 `31 - lane_id`，然后用 `T.tvm_warp_shuffle_xor(0xFFFFFFFF, v, i, 32, 32)` 做 5 轮蝶形归约（`i` 从 16 每轮减半，`while i >= 1`），shuffle 直译为 `__shfl_xor_sync`。这个例子浓缩了本讲多个知识点：线程 ID 获取（`cta_id`/`warp_id`/`lane_id` 一行声明）、`while` 循环驱动归约树、warp 级集体 intrinsic。

**（3）其他家族与查阅入口。** [tirx_guide/language_reference/cuda/threads_sync.rst:L65-L68](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L65-L68) 提示完整清单在后端 API 参考（`tvm.backend.cuda`）——这也是本讲「手册」用法的关键：记住家族名，用时查参考。

**（4）内联原始 CUDA。** [tirx_guide/language_reference/cuda/threads_sync.rst:L118-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L118-L143) 给出 `T.cuda.func_call(name, *args, source_code=..., return_type=...)` 的用法：`SRC` 字符串里的 `__device__ __forceinline__` 函数被原样发射进生成的 CUDA 源，调用点被接线成普通函数调用。适用场景：某个操作既没有 tile primitive 也没有 intrinsic（例如自定义的数值小函数）。

**（5）真实内核中的对照。** hgemm_v1 里 `T.ptx.mbarrier.init`、`T.ptx.tcgen05.alloc`、`T.ptx.fence.proxy_async`、`T.ptx.fence.mbarrier_init`、`T.cuda.cta_sync`、`T.ptx.mbarrier.try_wait`、`T.ptx.tcgen05.wait.ld`、`T.ptx.tcgen05.relinquish_alloc_permit`、`T.ptx.tcgen05.dealloc` 全部属于这一层，散布于 [chapter_intro_tirx/index.md:L118-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L118-L168)。可以在读实践（4.4.4 与第 5 节）时逐条勾选。

#### 4.3.4 代码实践

**实践目标**：跑通参考中的 `warp_reduce` 例子，验证 intrinsic 的直译映射。

**操作步骤**：

1. 把 [tirx_guide/language_reference/cuda/threads_sync.rst:L46-L57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L46-L57) 的 `warp_reduce` 原样抄进文件 `warp_reduce.py`（顶部补 `from tvm.script import tirx as T`）。
2. 文件末尾加 `warp_reduce.show()`，运行查看 IR。
3. 有 GPU 时：按 u9-l2 回路编译并调用——`ex.mod` 传入一个 32 元素的 PyTorch 张量，断言每个位置都等于全 warp 求和值 \(\sum_{i=0}^{31}(31-i)=496\)；再用 `inspect_source()` 找到 `__shfl_xor_sync`。

**需要观察的现象**：IR 中 shuffle 调用的参数形式；CUDA 源中 `__shfl_xor_sync(0xFFFFFFFF, v_ptr[0], i_ptr[0], 32)` 一行与参考 L63 的对照。

**预期结果**：32 个位置全部写回同一个归约值 496（每 lane 的初始值 `31 - lane_id` 之和）；CUDA 源中出现 `__shfl_xor_sync`，掩码 `0xFFFFFFFF` 表示全 warp 参与。

**待本地验证**：无 GPU 环境只能完成步骤 1–2（IR 检视）；数值断言与 CUDA 源对照需在 Blackwell GPU 环境验证。

#### 4.3.5 小练习与答案

**练习 1**：`T.cuda.cta_sum(val, num_warps, scratch.ptr_to([0]))` 为什么需要 `scratch` 参数，而 `T.tvm_warp_shuffle_xor` 不需要？

**参考答案**：warp shuffle 在 warp 内通过寄存器交换数据（shuffle 指令直接在 lane 间搬数），不需要额外存储；CTA 级归约要跨越多个 warp，必须借助共享内存中的一块 scratch 区域做中转（各 warp 的部分和写到 SMEM 再合并）。这也呼应 u2-l2：跨 warp 通信必须经过 SMEM 这类共享空间。

**练习 2**：`Tx.gemm_async` 和 `T.ptx.tcgen05.commit` 都属于「发起 MMA」相关，为什么一个在 `Tx.*`、一个在 `T.ptx.*`？

**参考答案**：`Tx.gemm_async` 是 tile 操作——声明「做一整块矩阵乘」，会被编译器按 ⌈K/16⌉ 展开为一串 `tcgen05.mma` 指令（u9-l1）；`T.ptx.tcgen05.commit` 是底层 intrinsic——把该线程已发起的异步操作挂到 mbarrier 上记账，直译为一条 PTX 指令，无展开。前者是「做什么」（tile 语义），后者是「资源与同步」（底层辅助）。

**练习 3**：想注入一个 TIRx 完全不认识的自定义激活函数，该用哪条路径？写出关键调用形式。

**参考答案**：内联原始 CUDA——`T.cuda.func_call("my_act", x, source_code=SRC, return_type="float32")`，其中 `SRC` 是包含 `__device__ __forceinline__ float my_act(float x) {...}` 的字符串；源码被原样发射进生成的 CUDA，调用点接线成普通函数调用。

### 4.4 同步原语的协作范围与四个高频语义

#### 4.4.1 概念说明

现在把本讲最核心的一张地图画出来：**每条同步原语的协作范围**。参考开篇的原话值得记住：四种同步机制在 GEMM 和 Flash Attention 内核中反复出现，因为它们控制的是异步引擎和并行线程组，「误用任何一种通常导致静默数据损坏或死锁」。

按协作范围从小到大排列：

| 原语 | 映射 | 协作范围 | 语义 |
| --- | --- | --- | --- |
| `T.ptx.elect_sync()` | PTX `elect.sync` | warp 内选举 | 选出 warp 内**单个活跃 lane**（不是 lane 0） |
| `T.cuda.warp_sync()` | `__syncwarp` | warp（32 线程） | warp 内栅栏 |
| `T.cuda.warpgroup_sync(id)` | `bar.sync id, 128` | 一个 warpgroup（128 线程） | 命名屏障，按 ID 区分 |
| `T.cuda.cta_sync()` | `__syncthreads` | 整个 CTA | 块级栅栏 |
| mbarrier（init/arrive/try_wait） | PTX `mbarrier.*` | 由初始化的期望到达数决定 | 线程到达 + 异步引擎完成 + 字节计数的合账 |

注意 mbarrier 不在这条「线程层级」轴上：它的协作范围是**账本制**的——init 时登记期望到达数，谁 arrive 谁记账，可以混合线程到达、`tcgen05.commit` 的硬件到达与 TMA 的字节扣减（u8-l1）。

#### 4.4.2 核心流程

参考提炼的四个高频语义，每个都对应一类易错点：

1. **mbarrier 相位**：`try_wait(bar, phase)` 阻塞直到屏障的内部相位**不同于**调用者传入的 `phase` 参数。因此循环里复用屏障时，每次 wait 成功后必须翻转本地相位变量（`phase ^= 1`）。
2. **选举**：`elect_sync()` 选出的是 warp 内单个活跃 lane——不是 lane 0，也不是每 CTA 一个线程。要把发起权收敛到恰好一个线程，必须再配一个 warp 级守卫，标准模式是 `if warp_id == 0:` 后跟 `if T.ptx.elect_sync():`。
3. **命名 warpgroup 屏障**：硬件提供 16 个命名屏障（ID 0–15）；`warpgroup_sync(10)` 只同步一个 warpgroup 的线程；**同时活跃**的独立同步必须用不同 ID；某 ID 的前一次同步完成后可以复用。
4. **fence 排序**：fence 把「生产者的写」排到「消费者（往往是异步引擎）的读」之前，共三种常用：`fence.proxy_async("shared::cta")`（线程写 SMEM → async proxy 读）、`fence.mbarrier_init()`（屏障初始化 → 后续 arrive/wait）、`tcgen05.fence.after_thread_sync()`（tcgen05 写回边的保守排序）。

#### 4.4.3 源码精读

**（1）相位语义与翻转纪律。** [tirx_guide/language_reference/cuda/threads_sync.rst:L77-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L77-L84)：`try_wait` 的完成判据是屏障内部相位与传入 `phase` 参数**不同**；复用屏障时每次 wait 后必须 `phase ^= 1`，漏翻会让后来的 wait 为更早的相位返回，消费者在当前生产者/异步操作完成前就拿到数据。这是 u8-l2 相位复用理论的语言参考出处，也是 GEMM Step 2 章末练习的核心（u11-l3）。hgemm_v1 中的用法见 [chapter_intro_tirx/index.md:L135-L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L135-L151)：`phase_mma: T.int32 = 0` 声明本地相位变量，MMA 发起后全体线程 `T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)` 等待。

**（2）选举语义与标准模式。** [tirx_guide/language_reference/cuda/threads_sync.rst:L86-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L86-L90)：`elect_sync` 选 warp 内单个活跃 lane，**不是 lane 0、不是每 CTA 一个线程**；要收敛到单线程必须配 warp 级守卫，`if warp_id == 0:` + `if T.ptx.elect_sync():` 正是书中发起 `Tx.gemm_async` 与 `tcgen05.commit` 的模式。hgemm_v1 的实例见 [chapter_intro_tirx/index.md:L143-L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L143-L149)。与之对照，同一内核初始化 mbarrier 用的是 `if lane_id == 0:`（确定性选 lane 0，见 L120）——两种写法都满足「先 warp 级守卫、再单 lane 选择」的纪律，前者靠硬件选举，后者靠坐标判断。

**（3）命名屏障与 ID 纪律。** [tirx_guide/language_reference/cuda/threads_sync.rst:L92-L100](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L92-L100)：16 个 ID；同时活跃的独立同步用不同 ID（例 `warpgroup_sync(wg_id + 10)`）；ID 在前一次同步完成后可复用。书中的展开说明见 [chapter_gemm_advanced/index.md:L95-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L95-L105)：`warpgroup_sync(10)` lowering 为 `bar.sync 10, 128`——`10` 是屏障 ID、`128` 是要求的到达线程数；指令本身**不会自动识别 warpgroup**，它同步的是 WG0 仅仅因为只有 WG0 的 128 线程执行到这条语句且都用 ID 10。Step 7 只有一个回写 warpgroup 用 ID 10；Step 9 有两个回写 warpgroup，改用 `warpgroup_sync(wg_id + 10)` 分到 ID 10/11，避免两组到达被记到同一本账上。

**（4）三种 fence 的分工。** [tirx_guide/language_reference/cuda/threads_sync.rst:L102-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/threads_sync.rst#L102-L116) 的表格：

| Fence | 排序的内容 |
| --- | --- |
| `T.ptx.fence.proxy_async("shared::cta")` | 线程写入的共享内存 → 异步代理（TMA store / MMA）读取它之前 |
| `T.ptx.fence.mbarrier_init()` | mbarrier 初始化 → 后续 arrive / wait 使用该屏障之前 |
| `T.ptx.tcgen05.fence.after_thread_sync()` | tcgen05 写回边上的保守排序（Steps 7–9 添加；TMA→MMA 路径不需要） |

三者在 Step 7 中恰好各就各位：初始化段两连发 `fence.proxy_async` + `fence.mbarrier_init()` 后接 `cta_sync`（[chapter_gemm_advanced/index.md:L186-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L186-L188)）；回写段在 mbarrier wait 之后、`tcgen05.ld` 之前插 `fence.after_thread_sync()`（[chapter_gemm_advanced/index.md:L265-L267](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L265-L267)），写 `Dsmem` 之后、命名屏障之前插 `fence.proxy_async`（[chapter_gemm_advanced/index.md:L283-L285](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L283-L285)）。书中正文对三者的分工有一句精炼总结：mbarrier wait 确认 MMA 已完成，`fence.after_thread_sync()` 建立跨线程的 tcgen05 顺序，`tcgen05.wait.ld()` 确认异步 TMEM 加载已填充目的寄存器——三者等待的对象各不相同，见 [chapter_gemm_advanced/index.md:L118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L118)。

**（5）`T.filter`：把选举结果变成线程守卫。** Step 7 中生产者与消费者的持久循环包在 `if T.filter(lane_id, T.ptx.elect_sync()):` 里，见 [chapter_gemm_advanced/index.md:L221-L222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L221-L222) 与 [L236-L237](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L236-L237)。`elect_sync` 在 warp 内选出唯一 lane，`T.filter` 只让被选中的 lane 真正执行循环体（u13-l1 讲过：TMA load 与 MMA 都只需一个线程发起，其余 lane 掩蔽）。需要说明：`T.filter` 在语言参考文档中未单独列出条目，它是书中内核使用的写法，语义以 u13-l1 的解释与本处代码为准（待确认：若后续语言参考版本补充 `T.filter` 条目，以其为准）。

#### 4.4.4 代码实践

**实践目标**：完成本讲的主实践前半——标注 hgemm_v1 中每个 `if` 守卫与每条同步语句的语义与协作范围。

**操作步骤**：

1. 打开 [chapter_intro_tirx/index.md:L104-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L104-L168)（hgemm_v1 内核体）。
2. 制作三列表格：**代码行 / 语义 / 协作范围**。协作范围从 {单 lane、warp（32）、warpgroup（128）、CTA（全部线程）、账本制（mbarrier）} 中选，并注明「执行者」与「需要到达/等待的线程数」。
3. 对照下面给出的参考标注核对（这就是本实践的参考答案）：

| 行 | 语句 | 语义 | 协作范围 |
| --- | --- | --- | --- |
| [L119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L119) | `if warp_id == 0:` | 选中 4 个 warp 中的 0 号 | warp 级守卫，32 线程进入 |
| [L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L120) | `if lane_id == 0:` | 在 warp 0 内再选 lane 0 | lane 级守卫，恰好 1 线程 |
| [L121](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L121) | `T.ptx.mbarrier.init(..., 1)` | 初始化 MMA 屏障（期望 1 次到达） | 单线程执行；建的是 CTA 共享的账本 |
| [L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L122) | `T.ptx.tcgen05.alloc(...)` | 分配 512 列 TMEM，写回基地址 | warp 0 的 32 线程集体（warp 级指令） |
| [L124-L125](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L124-L125) | `fence.proxy_async` / `fence.mbarrier_init` | 排序 SMEM 写与屏障初始化先于后续使用 | 每线程各自执行（排序语义，无到达数） |
| [L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L126) | `T.cuda.cta_sync()` | 初始化对全员可见后放行 | CTA 级，128 线程全部到达 |
| [L138-L139](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L138-L139) | `Tx.cta.copy` ×2 | CTA 协作搬 A、B 进 SMEM | CTA 级 tile 操作（128 线程协作） |
| [L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L140) | `T.cuda.cta_sync()` | 拷贝写齐后才发起 MMA | CTA 级，128 线程全部到达 |
| [L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L143) | `if warp_id == 0:` | 0 号 warp 进入发起段 | warp 级守卫，32 线程 |
| [L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L144) | `if T.ptx.elect_sync():` | 硬件选举 warp 内唯一 lane | 恰好 1 线程 |
| [L145-L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L145-L149) | `Tx.gemm_async` + `tcgen05.commit` | 单线程发起 MMA 并把完成挂到 mma_bar | 发起 1 线程；执行在 Tensor Core（异步引擎） |
| [L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L151) | `T.ptx.mbarrier.try_wait(...)` | 全体等 MMA 完成 | 全体 128 线程各自观察账本（wait 只观察不记账） |
| [L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L158) | `Tx.wg.copy_async` | warpgroup 集体读 TMEM 入寄存器 | warpgroup 级，128 线程集体 |
| [L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L159) | `T.ptx.tcgen05.wait.ld()` | 等异步 ld 填好寄存器再使用 | 每线程各自等待自己的 ld |
| [L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L162) | `Tx.copy(D[...], Dreg_f16[:])` | 每线程写自己负责的一行输出 | 线程级，无需同步 |
| [L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L165) | `T.cuda.cta_sync()` | 回写全部完成、TMEM 读毕后才释放 | CTA 级，128 线程全部到达 |
| [L166-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L166-L168) | `if warp_id == 0:` + relinquish/dealloc | warp 0 释放 TMEM | warp 级（warp 集体指令），32 线程 |

**需要观察的现象**：标注过程中应发现 hgemm_v1 的守卫只有两档（warp 级 `warp_id`、lane 级 `lane_id`/`elect_sync`），所有 CTA 级集体操作都位于守卫之外——单 warpgroup 串行内核不需要按 warpgroup 划分角色。

**预期结果**：得到一张 18 行左右的完整标注表（上表即参考答案），并能对每一行回答「谁执行、谁等待、到达数是多少」。

**待本地验证**：本实践为源码阅读型，无需运行；若要在 GPU 上验证某条同步的必要性，可参照 4.2.4 的思想实验方法注释掉单条语句推演后果。

#### 4.4.5 小练习与答案

**练习 1**：`warpgroup_sync(10)` 的两个参数（调用形式上只显式传了 ID `10`）各自是什么？为什么它能只同步 WG0？

**参考答案**：`10` 是命名屏障 ID，`128` 是要求的到达线程数（lowering 为 `bar.sync 10, 128`，128 由 warpgroup 的 128 线程隐含）。它能只同步 WG0 不是因为指令会自动识别 warpgroup，而是因为**只有** WG0 的 128 线程会执行到这条语句且都用 ID 10——其他 warpgroup 走的是别的角色分支，根本不在这条语句的控制流里。

**练习 2**：Step 9 为什么要写 `warpgroup_sync(wg_id + 10)` 而不是两个回写组都用 `warpgroup_sync(10)`？

**参考答案**：Step 9 有两个回写 warpgroup **同时活跃**，若都用 ID 10，两组共 256 线程的到达会被记到同一个屏障账上，任意一组都可能被另一组的到达错误放行。用 `wg_id + 10` 分到 ID 10 和 11，两组各记各的账。ID 复用的前提是「前一次使用该 ID 的同步已完成」，两个并发组不满足此前提。

**练习 3**：hgemm_v1 中 `mbarrier.try_wait` 在守卫之外由全体 128 线程执行，128 次 wait 会不会把屏障的账本弄乱？

**参考答案**：不会。`try_wait` 只观察屏障状态、不产生到达、不修改账本（u8-l1：wait 只观察）。账本上的到达数由 init 时的期望值（此处为 1）约束，来自 `tcgen05.commit` 的硬件到达；128 个线程各自观察同一个相位翻转即可，这正是 arrive/wait 分离设计的用处。

## 5. 综合实践

**任务**：完成本讲的完整标注实践——对 GEMM Step 7（hgemm_v7）做一遍 4.4.4 中对 hgemm_v1 做过的工作，然后对比两个内核的同步清单，写一份「控制流与同步演进笔记」。

**具体步骤**：

1. 打开 [chapter_gemm_advanced/index.md:L151-L302](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L151-L302)（Step 7 完整内核）。
2. 用与 4.4.4 相同的三列格式（代码行 / 语义 / 协作范围）标注**每一个** `if`/`elif` 守卫与每条同步语句。至少应覆盖：
   - 角色守卫三连：`if wg_id == 1:`（[L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L205)）、内层 `if warp_id == 3:` / `elif warp_id == 0:`（[L206](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L206)、[L231](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L231)）、`elif wg_id == 0:`（[L259](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L259)）；
   - 两个 `T.filter(lane_id, T.ptx.elect_sync())`（[L221](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L221)、[L236](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L236)）；
   - 回写段的完整同步序列：`mma2ld.wait` → `fence.after_thread_sync` → `tcgen05.wait.ld` → `ld2mma.arrive` → `fence.proxy_async` → 第一道 `warpgroup_sync(10)` → 单线程 TMA store（`if warp_id == 0:`/`if lane_id == 0:` + `commit_group`/`wait_group`）→ 第二道 `warpgroup_sync(10)`（[L263-L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L263-L294)）；
   - 初始化段与清理段（[L175-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L175-L188)、[L296-L300](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L296-L300)）。
3. 回答三个对比问题（参考要点）：
   - **守卫档位的变化**：hgemm_v1 只有 warp/lane 两档守卫；Step 7 增加了 warpgroup 档（`wg_id`）做角色划分——这正是 warp 特化带来的控制流结构变化（u13-l1）。
   - **屏障初始化方式的变化**：hgemm_v1 手写守卫调原始 `T.ptx.mbarrier.init`；Step 7 顶层裸调高层包装 `*.init(count)` 依赖其自带守卫（4.2.3 的（2）（4））。
   - **CTA 级同步的位置变化**：hgemm_v1 有三道顶层 `cta_sync` 守护三道交接（u11-l2）；Step 7 只剩初始化后与清理前两道位于角色分支**之外**的 `cta_sync`，角色分支内部一律改用命名屏障 `warpgroup_sync(10)` 与 mbarrier——因为角色分支是 warpgroup 级发散的，`cta_sync` 放进去必死锁（4.2.3 的（5））。
4. （可选，有 Blackwell GPU 时）把 Step 7 的某一道 `warpgroup_sync(10)` 改成 `cta_sync()` 编译运行，观察挂死现象，验证 4.2.4 的推演；无 GPU 时跳过，在笔记中记录该思想实验的推演结论即可。

**预期结果**：一份约 25 行的 Step 7 标注表 + 三个对比问题的回答。检验标准：表中每一行都能不假思索地说出「谁执行、谁等待、到达数是多少、放错位置会怎样」。

**待本地验证**：步骤 4 的实际挂死表现需本地验证；步骤 1–3 为源码阅读型，可直接完成。

## 6. 本讲小结

- TIRx 控制流是「薄」的直译层：`if`/`else`、四种循环（`T.serial`/`T.unroll`/`T.vectorized`/`T.grid`）、`while` 各自映射到对应的 CUDA 结构，`T.if_then_else` 是不产生分支的表达式级选择；编译器不会替你添加守卫。
- 均匀性是集体操作的生死线：`cta_sync` 必须被整个 CTA 一致到达，放进线程级或 warpgroup 级发散分支必死锁；只想同步一个 warpgroup 时用命名屏障 `warpgroup_sync(id)`。
- 屏障初始化有两条路：高层 `MBarrier.init()` 包装自带单线程守卫（但别再嵌进发散分支），原始 `T.ptx.mbarrier.init` 不加守卫、必须自己选初始化线程——hgemm_v1 与 Step 7 恰好各用一条。
- `T.cuda.*`/`T.ptx.*` 是 tile 操作之下的底层辅助层，全部同步原语住在这里；再往下还有 `T.cuda.func_call` 内联原始 CUDA 兜底。
- 四个高频语义各自的坑：mbarrier 相位（`try_wait` 等「不同于传入 phase」，复用必须 `phase ^= 1`）、选举（`elect_sync` 选 warp 内单 lane 而非 lane 0，配 warp 守卫才收敛到单线程）、命名屏障 ID（同时活跃的同步用不同 ID，完成后可复用）、fence 三种（`proxy_async`/`mbarrier_init`/`after_thread_sync` 各排各的边）。
- 拿着「代码行 / 语义 / 协作范围」三列表格去读内核，hgemm_v1 与 Step 7 的每一行守卫与同步都能落到本讲清单上——这张表也是读 FA4 内核（u14 系列）同一套方法。

## 7. 下一步学习建议

- **下一篇（u15-l3）**：进入编译器内部——TIRx lowering pipeline。本讲看到的「直译」并非天然成立：`LowerTIRx` 如何决定展开、`FlattenBuffer` 与 `SplitHostDevice` 如何加工这些控制流节点，是下一讲的主题；届时可回头验证 4.1 的映射发生在流水线的哪一步。
- **回读内核**：带着本讲的标注表重读 u13-l1（Step 7）与 u14-l2（FA4 的角色与屏障协议），你会发现 FA4 的 `p_o_rescale` 期望 256 次到达等设计，本质仍是本讲的「守卫集合 = 到达数账本」检查。
- **查阅习惯**：语言参考（`tirx_guide/language_reference/`）与后端 API 参考（`tvm.backend.cuda`）是两本字典，本讲建立的索引——控制流查 control_flow.rst、同步语义查 threads_sync.rst、intrinsic 全集查后端 API——值得在后续每次读内核时反复使用。
- **调试衔接**：当你按 u15-l7（调试 warp-specialized 内核）排查死锁/静默错果时，本讲的均匀性检查三步法（标集体操作 → 找守卫 → 比集合）就是那张 worksheet 里「交接」一栏的具体操作。
