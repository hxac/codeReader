# instruction_fetch 与 page_allocator

## 1. 本讲目标

在 [U6·L1] 里我们看到，controller 这台「VM 大脑」对每条指令只做四件事：**取指 → 建立物理页序 → 构造信号量 → 通知就绪**。本讲把其中的前两步单独抽出来，做一次「显微级」的源码精读。

学完后你应该能够：

1. 解释 `load_instructions` 如何用一个 warp 的 32 个 lane，以**一次合并（coalesced）的全局内存事务**把一条 32 个 `int`（128 字节）的指令读进共享内存。
2. 说清「逻辑页 lid」与「物理页 pid」的区别，并写出 controller 计算 `pid_order` 时所用的**递推公式**。
3. 解释为什么 page_allocator 在为第 n 条指令排定页序时，要去读**第 n−1 条指令的 opcode**，并说明 `release_lid` 是如何「复用」上一条指令的 `pid_order` 表的。
4. 手算一个具体的 `lid → pid` 映射过程（以 NoOp 与 PartialAttention 两个 op 为例）。

> **关于两份代码的说明（先读这段）**：本讲的两个主角文件 `instruction_fetch.cuh` 和 `page_allocator.cuh` 各提供了一个**独立的、自包含的循环函数** `instruction_fetch_loop` / `page_allocator_loop`。但在当前仓库里，这两步的实际工作是被**内联**在 [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 的 `main_loop` 中的（Step 1 与 Step 2）。全仓库检索可以确认：`instruction_fetch_loop` 和 `page_allocator_loop` **都没有被任何地方实例化调用**——例如 `instruction_fetch_loop` 对 `load_instructions` 的调用多传了一个实参（见下文 4.1.3），而 `load_instructions` 只接受 3 个参数，一旦被实例化就无法编译。因此本讲把它们当作「把 controller 的某一步单独抽出来、便于逐行学习」的**等价参考实现**来讲；真正跑在生产路径上的是 `controller.cuh::main_loop`，我们会在每一节同时给出两处的对照行号。

## 2. 前置知识

在继续之前，请确认你已掌握以下概念（均来自前几讲）：

- **指令与 opcode**：controller 从全局内存（gmem）把一条条「指令」取到共享内存（smem）。每条指令是一个定长 `int` 数组，**第 0 个元素就是 opcode**（操作码），其余元素是该 op 的参数。`INSTRUCTION_WIDTH = 32`，即每条指令 = 32 个 `int` = 128 字节（见 [include/config.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L15)）。
- **warp 与 lane**：一个 warp = 32 个线程（lane），`laneid` 取值 0..31。Megakernels 的 controller 本身就是**一个 warp**，本讲里所有「每个 lane 干一件事」的写法都发生在 controller warp 内部。
- **指令流水与 ring/phase**：指令在共享内存里被放在一个深度为 `INSTRUCTION_PIPELINE_STAGES`（=2）的环形 buffer 里，由游标 `instruction_index`（绝对指令号）与 `instruction_ring`（槽位号 = `index % 2`）共同定位。同一槽位每被复用一次，「相位 phase」翻转一次，用来区分信号量的不同轮次。详见 [U6·L1] 与 [U5·L3] 4.2。
- **逻辑页 lid / 物理页 pid / `pid_order`**：Megakernels 把动态共享内存切成 `NUM_PAGES`（=13）个等大的「物理页」`page`。op 代码里用「逻辑页 lid」来称呼它想要的第几页，而 `pid_order[lid]` 把这个逻辑号翻译成真正的物理页号 pid。`pid_order` 存在每条指令自己的 `instruction_state_t` 槽里（每指令一份）。详见 [U5·L3]。
- **`dispatch_op`**：一个可变参数模板，根据 `opcode` 在编译期把调用派发到匹配的 op 子结构。详见 [U5·L4]。

一句话心智模型：**取指**是把 gmem 里的指令「搬」进当前槽位的 `instructions` 数组；**分页**是算出本条指令的 `pid_order` 表。前者回答「这条指令长什么样」，后者回答「这条指令的 13 个逻辑页分别落在哪 13 个物理页上」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/controller/instruction_fetch.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh) | 定义 `load_instructions`（L11-L27，**真正被使用的取指函数**）与参考循环 `instruction_fetch_loop`（L29-L51）。 |
| [include/controller/page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh) | 定义 `page_allocator_op_dispatcher`（L10-L19，把派发桥接到 op 的 `release_lid`）与参考循环 `page_allocator_loop`（L21-L71）。 |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | 生产路径：`main_loop`（L15）内联了取指（Step 1，L66-L72）与分页（Step 2，L74-L103）。 |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `instruction_state_t`（含 `pid_order`，L11-L19）、`dispatch_op`（L32-L55）、`ring_advance`（L57）、`state::pid(lid)`（L150-L154）与 `await_instruction`（L122-L127）。 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `INSTRUCTION_WIDTH=32`、`NUM_PAGES=13`、`INSTRUCTION_PIPELINE_STAGES=2` 等常量。 |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | NoOp 的 `release_lid`（L13-L17，返回 `query`，恒等映射）——最简单的分页参照。 |
| [demos/low-latency-llama/attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) | PartialAttention 的 `release_lid`（L276-L282，旋转置换）——一个有实际页轮换的例子。 |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | `release_lid` 的「按 `iters` 余数选不同 `ret_order`」版本（L70-L102）——展示 release_lid 可以**依赖指令参数**。 |

## 4. 核心概念与源码讲解

### 4.1 instruction_fetch_loop：把一条指令读进共享内存

#### 4.1.1 概念说明

「取指（instruction fetch）」要做的事情非常朴素：把第 `n` 条指令的 32 个 `int`，从 gmem 里的指令表 `g.instructions`，复制到当前流水槽位的 `instructions` 数组里。

难点不在于「做什么」，而在于「**怎么用最便宜的方式做**」。controller 是一个 warp（32 个线程），而一条指令恰好是 32 个 `int`。最自然的做法就是：**让第 i 个 lane 负责第 i 个 `int`**。这样 32 个 lane 同时发起读取、地址连续，GPU 就能把它们合并成**一次** 128 字节的全局内存事务（coalesced load），而不是 32 次零碎访问。

这正是 `load_instructions` 干的事。它的核心只有一行：`instruction[laneid] = src_ptr[laneid];`。

#### 4.1.2 核心流程

```
对 controller warp 内的每个 lane（laneid = 0..31）：
  1. src_ptr = g.instructions 里「本 worker、本 instruction_index」那条指令的首地址
  2. 若 laneid < INSTRUCTION_WIDTH(=32)：
        instruction[laneid] = src_ptr[laneid]   // 每 lane 拷一个 int
  → 32 个 lane 的拷贝合并成一次 128 字节的 coalesced gmem → smem 传输
```

几个关键量：

- 每条指令的体积：\(32 \times 4\text{B} = 128\text{B}\)。
- 一次合并事务覆盖的地址范围：恰好 128 字节（一个 sector burst），所以**一个 warp 一拍读完一整条指令**。
- 这也是为什么会有 `static_assert(INSTRUCTION_WIDTH <= 32)`：warp 只有 32 个 lane，指令宽度不能超过 lane 数，否则一个 lane 得读多个元素、不再是「一 lane 一 int」的清爽模型。

`instruction_fetch_loop` 则是把上面的取指放进「指令流水」骨架：逐条推进 `instruction_index`，复用前要先 `wait` 上一轮该槽位的 `instruction_finished`，取指后 `arrive` 本轮的 `instruction_arrived` 通知下游。这套 ring/phase 节拍与 [U6·L1] 完全一致，本节不再重复，只聚焦取指本身。

#### 4.1.3 源码精读

**`load_instructions`（真正被使用的取指函数）**：

[include/controller/instruction_fetch.cuh:11-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L11-L27) 定义了它。逐行看：

- [L14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L14)：取本 lane 的编号 `laneid`。
- [L16-L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L16-L17)：算出源地址 `src_ptr`。它把 `get_worker_id()`（= SMID，见 [include/util.cuh:27-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L27-L30)）和 `instruction_index` 作为坐标，送进 `g.instructions`（类型见 [include/config.cuh:54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L54) 的 `kittens::gl<int,1,-1,-1,INSTRUCTION_WIDTH>`），取回当前这条指令那一行的起始 `int*`。坐标到具体轴的映射遵循 ThunderKittens 的 `gl`/`coord` 语义（本仓库 `ThunderKittens/` 子目录未检出，故此处只描述 Megakernels 这一侧可观察到的用法，不展开 TK 内部）。
- [L22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L22)：`static_assert(INSTRUCTION_WIDTH <= 32)`——保证「一 lane 一 int」模型成立。
- [L24-L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26)：核心拷贝。`instruction` 指向当前流水槽位的 `instructions[32]`（共享内存），每个 lane 写一个元素。32 个连续地址的读 + 连续地址的写，两端都是完美合并的。

**生产路径的调用点**：

[include/controller/controller.cuh:66-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L66-L68) 是 Step 1「Load instructions (no semaphores used)」，用**正确的 3 个参数**调用 `load_instructions`：

```cpp
load_instructions<config, globals>(&kvms.instruction()[0],
                                   kvms.instruction_index, g);
```

注意 `&kvms.instruction()[0]` —— `instruction()` 是 `state` 的访问器，返回当前 `instruction_ring` 槽位里的 `instructions` 数组（见 [include/util.cuh:83-89](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L83-L89)）。

**参考循环 `instruction_fetch_loop`（未被实例化）**：

[include/controller/instruction_fetch.cuh:29-51](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L29-L51) 把取指包进流水骨架：循环头 [L35-L40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L35-L40) 推进 `instruction_index` 与 `instruction_ring`；[L41-L46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L41-L46) 是「槽位复用前先等上一轮完成」的标准 phasebit 等待（与 [U6·L1] 一致）；最后 [L47-L49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L47-L49) 调用 `load_instructions`。

> **诚实提示**：这个调用 [L47-L49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L47-L49) 传了 **4 个**实参（多了一个 `kvms.instruction_arrived[...]`），而 `load_instructions` 只声明了 3 个形参。这意味着 `instruction_fetch_loop` 在当前形态下**无法通过编译**，也印证了它没有被纳入生产构建——真正干活的是上面 `controller.cuh` 内联的那 3 参数调用。我们把它当作「取指步骤的伪代码骨架」来读即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一 lane 一 int」的合并读取，并理解 `INSTRUCTION_WIDTH <= 32` 这个断言的来历。

1. **操作步骤**：
   - 打开 [include/controller/instruction_fetch.cuh:24-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26)，确认每个 lane 只读 `src_ptr[laneid]` 一个 `int`。
   - 在 [include/config.cuh:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14) 确认 `INSTRUCTION_WIDTH = 32`。
   - 算账：\(32 \text{ lane} \times 4\text{B} = 128\text{B}\)，正好是一条指令的体积，也正好是一次 128 字节合并事务的范围。
2. **需要观察的现象**：思考题——假设有人把 `INSTRUCTION_WIDTH` 改成 48（**仅作思考，不要真改源码**），`static_assert` 会如何？哪怕不断言，`instruction[laneid] = src_ptr[laneid]` 在 lane 32..47 范围内会发生什么？
3. **预期结果**：`static_assert(48 <= 32)` 编译期失败。即便绕过断言，laneid 只到 31，lane 32..47 对应的元素根本没人读——指令的后 16 个 `int` 会丢失。所以「指令宽度 ≤ warp 宽度」是这个取指模型的硬约束。若指令真要更宽，就得让每个 lane 读多个元素（循环展开），代码也会相应变复杂。
4. 「待本地验证」：以上为源码阅读型推理，无需运行；若想观察合并效率，可在 `load_instructions` 前后用 `s.record(...)`（见 [include/util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)）打点并用 nsys/nsight-compute 观察那次全局加载的吞吐（属于示例性练习，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_instructions` 里要用 `laneid` 作为「谁读第几个 int」的依据，而不是用 `threadIdx.x`？

> **参考答案**：controller 是一个 warp，`laneid = threadIdx.x % 32` 在单 warp 内等价于 `threadIdx.x`，但语义上更准确地表达了「这是 warp 内的第几个 lane」。整个取指是 **warp 级协同**——32 个 lane 必须各自负责一个连续的 `int` 才能合并成一次事务，用 `laneid` 强调了「按 lane 切分这条指令」的意图，也让代码在概念上能直接和「32-lane 的合并访问」对应。

**练习 2**：取指阶段为什么注释里特意写 `(no semaphores used)`？

> **参考答案**：取指只是 controller warp 内部「从 gmem 拷 32 个 int 到本槽位」，全程在同一个 warp 内完成、没有跨 warp 交互，所以不需要任何 `kittens::semaphore`。信号量是后面 Step 2/3 以及与其他 worker（loader/storer/consumer）交接时才用到的。强调「no semaphores」是在提醒读者：这一步是最「干净」的一步。

---

### 4.2 page_allocator_loop：建立本条指令的物理页序

#### 4.2.1 概念说明

取指之后，controller 知道了「这条指令长什么样」（opcode + 参数）。下一步要回答一个对流水至关重要的问提：**这条指令的 13 个逻辑页，分别该落在哪 13 个物理页上？** 答案就是 `pid_order[0..12]`。

为什么这件事很重要、又为什么不能写死？因为 Megakernels 是**深度流水**的——当前指令还在被 consumer 计算时，下一条指令的 loader 可能已经在往「同一批物理页」里灌新数据了。物理页是稀缺资源（只有 13 个，每个 16 KiB，见 [include/config.cuh:42-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L42-L44)），必须**在指令之间复用**。于是 controller 需要为每条指令排定一份 `pid_order`，告诉所有 worker：「本条指令里，逻辑页 0 用物理页 X，逻辑页 1 用物理页 Y……」。

关键的洞见是：**物理页是在指令之间「传递」的**。当第 n−1 条指令的 op 用完某个物理页、把它释放掉时，第 n 条指令就可以接手这个物理页。所以第 n 条指令的页序，应当从第 n−1 条指令的页序「继承」而来——而具体怎么继承，取决于第 n−1 条指令是哪种 op（不同 op 释放页的顺序不同）。这就是 `release_lid` 登场的地方。

#### 4.2.2 核心流程

controller 为第 n 条指令（n = `instruction_index`）计算 `pid_order` 的规则：

```
若 n == 0（第一条指令）：
    pid_order_0[lid] = lid                       // 恒等：逻辑页 i 就用物理页 i
否则（n >= 1）：
    opcode_{n-1} = 第 n-1 条指令槽位里的 instructions[0]   // 看「上一条」的 opcode
    对每个 lid（= laneid，0..NUM_PAGES-1）：
        donor_lid = op(opcode_{n-1})::release_lid(lid)    // 该 op 给出的「捐赠者」逻辑页号
        pid_order_n[lid] = pid_order_{n-1}[ donor_lid ]    // 继承上一条的物理页
```

写成数学递推：

\[
\text{pid\_order}_n[\ell] \;=\; \text{pid\_order}_{n-1}\!\big[\;\text{release\_lid}_{\,opcode_{n-1}}(\ell)\;\big], \qquad n \ge 1
\]

基础情形：

\[
\text{pid\_order}_0[\ell] = \ell
\]

**不变量（重要）**：只要每个 op 的 `release_lid` 都是 \(\{0,\dots,P-1\}\)（\(P=\) `NUM_PAGES`）上的**双射**（一一置换），那么由「置换 ∘ 置换」还是置换可知，每条指令的 `pid_order` 都是一个合法置换——即 13 个逻辑页映射到 13 个**互不相同**的物理页，绝不会有两个逻辑页撞到同一个物理页。后面会看到 NoOp、PartialAttention 的 `release_lid` 都满足这个性质。

相位与流水：和取指一样，page_allocator 在复用槽位前要先 `wait` 上一轮的 `instruction_finished`；算完后让 lane 0 `arrive` 本轮的 `instruction_arrived`，把「页序已就绪」告诉下游 worker。下游 worker 随后通过 `state::pid(lid)`（一次 `lds`，见 4.3）把这张表读出来用。

#### 4.2.3 源码精读

**`page_allocator_op_dispatcher`（生产路径也用它）**：

[include/controller/page_allocator.cuh:10-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L10-L19) 定义了这个「桥接 functor」。它的 [L13-L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L13-L17) `run` 把调用转给 `op::controller::release_lid(g, instruction, query)`。配合 [include/util.cuh:32-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L32-L55) 的 `dispatch_op`，就能按 `opcode` 选到正确的 op 并调用其 `release_lid`。

**参考循环 `page_allocator_loop`**：

[include/controller/page_allocator.cuh:21-71](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L21-L71)。逐段看：

- [L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L26)：`membermask = 0xFFFFFFFF >> (32 - NUM_PAGES)`——只让低 `NUM_PAGES`(=13) 个 lane 参与，因为只有 lid 0..12 有意义。
- [L41-L43](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L41-L43)：**基础情形**。第 0 条指令，`next_pid = laneid()`，即 `pid_order_0[lid] = lid`。
- [L44-L65](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L44-L65)：**递推情形**。这是本讲的核心：
  - [L45-L48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L45-L48)：算出「上一条指令」的槽位号 `last_instruction_ring`（= 当前 ring 回退一步）。
  - [L49-L52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L49-L52)：`wait(instruction_arrived[last_instruction_ring])`——确保上一条指令的取指/分页数据已就位（opcode 和 `pid_order` 都可读）。
  - [L54-L55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L54-L55)：读**上一条指令的 opcode**：`opcode = all_instructions[last_instruction_ring].instructions[0]`。
  - [L56-L62](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L56-L62)：用这个 opcode 派发到对应 op 的 `release_lid`，`query = lane`（= 当前 lid），得到 `lid`（变量名复用了，这里指「捐赠者逻辑页号」）。
  - [L63-L64](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L63-L64)：**继承**——`next_pid = all_instructions[last_instruction_ring].pid_order[lid]`，即「上一条指令里那个捐赠者逻辑页所对应的物理页」。
- [L66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L66)：把结果写进本条指令的 `pid_order()[laneid] = next_pid`。
- [L67](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L67)：`bar.warp.sync %0`（`membermask`）——在通知下游前，先同步参与写 `pid_order` 的 13 个 lane，保证写入对全 warp（及随后读 `pid()` 的 worker）可见。
- [L68-L69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L68-L69)：lane 0 `arrive(instruction_arrived[ring])`，宣布本条指令（含页序）已就绪。

**生产路径的等价内联（Step 2）**：

[include/controller/controller.cuh:74-99](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L74-L99) 做的是完全一样的事，只是写进了一个 warp 的 `main_loop`：

- [L79-L82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L79-L82)：基础情形 `pid_order()[laneid] = laneid`（带 `laneid < NUM_PAGES` 守卫）。
- [L84-L85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L84-L85)：读上一条指令的 opcode `last_opcode = all_instructions[last_instruction_ring].instructions[0]`。
- [L87-L94](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L87-L94)：派发 `release_lid`，`query = lane`。
- [L96-L97](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L96-L97)：`pid_order()[lid] = all_instructions[last_instruction_ring].pid_order[lid]`。

> **两处差异（顺带理解）**：① 生产路径用 `if (laneid < NUM_PAGES)` 守卫写入，参考循环用 `membermask` 只同步前 13 个 lane，二者等价地避免了 lane 13..31 往 `pid_order` 的 padding 区写垃圾（padding 区从不被读，见 [U5·L3]）。② 参考循环里有 `wait(instruction_arrived[last_instruction_ring])`，生产路径没有——因为生产路径里取指和分页在**同一个 controller warp 的同一次遍历**里顺序发生，上一条指令的数据就是本 warp 上一轮自己写的，不存在跨 warp 可见性问题；参考循环写成自包含「阶段」，所以加了一道防御性等待。

#### 4.2.4 代码实践

**实践目标**：回答本讲核心问题之一——**page_allocator 为什么非要读「上一条指令」的 opcode，而不是当前这条的？** 并追踪一次 `lid → pid` 的映射。

1. **操作步骤（解释「为什么是上一条」）**：
   - 重读 [include/controller/controller.cuh:84-97](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L84-L97)。注意它读的是 `last_instruction_ring`（上一条）的 opcode，并把结果写成「上一条的 `pid_order[...]`」。
   - 用一句话写下你的解释，再对照下面的参考答案。
2. **操作步骤（追踪 lid→pid）**：以 NoOp 作为第 0 条指令、PartialAttention 作为第 1 条指令（即第 1 条指令分页时，其「上一条」opcode = NoOp）。
   - NoOp 的 `release_lid` 见 [include/noop.cuh:13-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L13-L17)：`return query;`（恒等）。
   - 计算 `pid_order_1[lid] = pid_order_0[ release_lid_NoOp(lid) ] = pid_order_0[lid] = lid`。
3. **需要观察的现象**：因为 NoOp 是恒等映射，第 1 条指令的 `pid_order` 仍是恒等。
4. **预期结果**：`pid_order_1 = {0,1,2,3,4,5,6,7,8,9,10,11,12}`，与第 0 条完全相同。这印证了「上一条是 NoOp（不持有/立即释放所有页）时，页序不发生轮换」。下一节 4.3 会用 PartialAttention 的旋转 `release_lid` 做一个会真正变化的追踪。
5. 「待本地验证」：以上为纸笔推导，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么递推公式里用的是 `pid_order_{n-1}[donor_lid]`，而不是「从某个全局空闲页池里取一个 pid」？

> **参考答案**：因为物理页的复用是**链式继承**的：第 n 条指令直接接手第 n−1 条指令用过的物理页。第 n−1 条的 `pid_order` 已经记录了「它的逻辑页 → 物理页」的完整映射，所以只要知道「我想接手第 n−1 条的哪一个逻辑页」（即 `donor_lid`），就能查到对应的物理页号。这样做的好处是：页的归属在**编译期 + 取指期**就被静态排定成一条确定的链，运行时只需用信号量保证「上一家真正用完了」即可（见 4.3.2 的 `wait_page_ready`/`finish_page`），无需任何运行期分配器或锁。

**练习 2**：第一条指令为什么直接用恒等映射 `pid_order_0[lid] = lid`？这个选择会限制后续指令吗？

> **参考答案**：第一条指令没有「上一条」可继承，所以需要一个确定的起点；恒等映射（逻辑页 i = 物理页 i）是最自然、最不容易出错的起点。它不会限制后续指令——后续每条指令的 `pid_order` 都是「起点置换」不断被各 op 的 `release_lid` 复合的结果，换一个起点只会让整条链整体平移，物理页的**相对**复用关系不变。由于所有物理页等价（都是 16 KiB 的同构 `page`），起点选恒等是最干净的。

---

### 4.3 release_lid 与 pid_order：页回收 → 页复用的「交接时刻表」

#### 4.3.1 概念说明

`release_lid` 是每个 op 在其 `controller` 子结构里必须提供的一个**静态函数**，签名形如：

```cpp
static __device__ int release_lid(const globals &g,
                                  typename config::instruction_t &instruction,
                                  int &query);
```

它回答的问题是：**「当本 op（作为第 n−1 条指令）执行完毕，对于第 n 条指令里编号为 `query` 的逻辑页，应当把本 op 的哪一个逻辑页（及其背后的物理页）交接给它？」** 返回值就是这个「捐赠者逻辑页号」`donor_lid`。

换句话说，`release_lid` 是 op 作者手写的一张**页交接时刻表**：它声明了本 op 在生命周期结束时，如何把自己的物理页「传递」给下一条指令。controller 不知道也不关心具体业务，它只负责机械地套用递推公式（4.2.2）把所有 op 的 `release_lid` 串成一条完整的页复用链。

为什么要读「上一条」的 opcode？因为 `release_lid` 是**属于上一条指令那个 op 的**——是上一条指令的 op 在释放页，所以交接规则由上一条指令的 opcode 决定。当前指令的 opcode 决定的是「当前指令自己将来怎么释放页」（会影响再下一条），而不是「当前指令怎么从上一条继承页」。

#### 4.3.2 核心流程

完整闭环（从 controller 排定，到 worker 消费，再到回收）：

```
controller（取指期，提前排定）：
    pid_order_n[lid] = pid_order_{n-1}[ release_lid_{n-1}(lid) ]
    写进 all_instructions[ring_n].pid_order
        ↓ arrive(instruction_arrived[ring_n])
worker（执行期，读取映射）：
    await_instruction()：缓存 pid_order[0] 的共享地址 → pid_order_shared_addr
    pid(lid)：lds 读 pid_order_shared_addr + lid*4 → 得到物理页号 pid
    用 pages[pid] / page_finished[pid] 进行 load/consume/store
        ↓
回收（动态同步，保证「上一家用完才轮到我」）：
    上一家的 loader/consumer 用完某页 → finish_page(pid, count)
    本家的 worker 用页前 → wait_page_ready(pid)   // 阻塞直到上一家 finish
```

关键点：`pid_order` 是**静态的、提前算好的**交接计划；而「上一家是否真的用完了」是**动态的**，由 `page_finished` 这组信号量在运行时保证。两者结合，既有了编译期可分析的确定性页复用链，又保证了运行时的数据竞争安全（见 [include/util.cuh:150-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L168) 的 `pid` / `wait_page_ready` / `finish_page`）。

#### 4.3.3 源码精读

**最简单的 op：NoOp（恒等交接）**：

[include/noop.cuh:13-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L13-L17) 的 `release_lid` 直接 `return query;`。语义：NoOp 不持有任何有意义的页，它把「第 query 个逻辑页」原样交接给下一条的第 query 个逻辑页，于是 `pid_order` 保持不变。配合 NoOp 的 loader（[include/noop.cuh:24-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L33)，「Release all pages, ASAP」），NoOp 一上来就把所有页 `finish_page` 掉，相当于一个「清空流水」的占位 op。

**有实际轮换的 op：PartialAttention（旋转置换）**：

[demos/low-latency-llama/attention_partial.cu:276-282](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L276-L282) 给了一个非平凡的 `release_lid`：

```cpp
int ret_order[13] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0, 1};
return ret_order[query];
```

即 \(\text{release\_lid}(q) = (q + 2) \bmod 13\)，一个循环移位 2 的置换。它满足双射性（4.2.2 的不变量成立）。

**能依赖指令参数的 op：matvec_pipeline（按 `iters` 余数选不同置换）**：

[demos/low-latency-llama/matvec_pipeline.cuh:70-102](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L70-L102) 展示了 `release_lid` 还可以**读指令参数**：它解析出 `inst.iters`（迭代次数），根据 `iters == 1` / `iters == 2` / `iters % 3` 的余数，选择**不同**的 `ret_order`（[L86-L101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L86-L101)）。这说明交接时刻表不必是固定的——同一个 op，不同参数下可以有不同的页释放顺序，以匹配三级 input/output 流水的尾部收尾需求（详见 u8 流水讲义）。

**消费这张表的入口：`state::pid(lid)` 与 `await_instruction`**：

- [include/util.cuh:122-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L127)：`await_instruction()` 先 `wait(instruction_arrived[ring])`（等 controller 算完 `pid_order` 并通知），然后把 `pid_order[0]` 的共享地址缓存到 `pid_order_shared_addr`。
- [include/util.cuh:150-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L154)：`pid(lid)` 用一条 `lds` 从 `pid_order_shared_addr + lid*4` 读出物理页号。这就是 op 代码里到处可见的 `s.pid(ACTIVATION_PAGE)` 之类调用的最终落点。

#### 4.3.4 代码实践

**实践目标**：用一个真正会改变页序的 op，完整追踪一次 `lid → pid` 的映射，并验证置换不变量。

设定：第 0 条指令是任意 op（`pid_order_0` = 恒等），第 1 条指令的**上一条**是 PartialAttention（opcode 来自第 0 条），第 2 条指令的**上一条**仍是 PartialAttention（opcode 来自第 1 条）。我们要算 `pid_order_1` 和 `pid_order_2`。

1. **操作步骤**：
   - 已知 `pid_order_0 = {0,1,2,...,12}`（恒等），PartialAttention 的 `release_lid(q) = (q+2) % 13`。
   - 算 `pid_order_1`：对每个 lid，`pid_order_1[lid] = pid_order_0[(lid+2)%13] = (lid+2)%13`。
   - 算 `pid_order_2`：对每个 lid，`pid_order_2[lid] = pid_order_1[(lid+2)%13] = ((lid+2)+2)%13 = (lid+4)%13`。
2. **需要观察的现象**：每次都把整张表向「值 +2」的方向旋转一次；两次复合后相当于旋转 4。
3. **预期结果**：
   - `pid_order_1 = {2,3,4,5,6,7,8,9,10,11,12,0,1}`
   - `pid_order_2 = {4,5,6,7,8,9,10,11,12,0,1,2,3}`
   - 校验不变量：两张表的 13 个值都是 0..12 的一个排列（无重复），满足「13 个逻辑页落在 13 个互不相同的物理页」。
4. 「待本地验证」：以上为纸笔推导。若想在设备上核对，可在 controller 的 Step 2 之后（[include/controller/controller.cuh:101-103](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L101-L103) 附近）由 lane 0 把本条 `pid_order` 用 `printf` 打出来（需 `MK_DEBUG`/调试构建，属于示例代码，勿提交），再与上表对照。

#### 4.3.5 小练习与答案

**练习 1**：如果某个 op 的 `release_lid` 不是双射（比如 `return 0;` 对所有 query 都返回 0），会发生什么？

> **参考答案**：递推 `pid_order_n[lid] = pid_order_{n-1}[release_lid(lid)]` 会让多个 lid 映射到**同一个**物理页（都拿到 `pid_order_{n-1}[0]`）。这意味着该指令的两个逻辑页会撞到同一块物理共享内存，loader 灌进去的数据会互相覆盖，consumer 读到的就是错的。所以 op 作者必须保证 `release_lid` 是置换；代码里没有运行期检查，这是一个**隐含的契约**。

**练习 2**：`pid_order` 存在每条指令自己的 `instruction_state_t` 槽里（而不是全局唯一一张表），和「读上一条的 `pid_order`」这件事有什么关系？

> **参考答案**：正因为每条指令有自己的 `pid_order` 副本，page_allocator 才能在为第 n 条指令算新表的同时，**仍然读得到**第 n−1 条指令那张尚未被覆盖的旧表（它们在两个不同的 ring 槽里）。这与流水的「多条指令同时在飞」直接相关：若只有一张全局表，算第 n 条时就会把第 n−1 条（可能还在被 consumer 用）的映射冲掉。详见 [U5·L3] 练习 2。

**练习 3**：`release_lid` 的返回值是「逻辑页号」而不是「物理页号」，这个抽象分层带来了什么好处？

> **参考答案**：让 op 作者只需关心「**逻辑上**我把第几页交给下一条」，而无需知道、也无法知道这些逻辑页当前实际落在哪个物理页（那是 controller 链式递推出来的、随上下文变化的）。op 的 `release_lid` 因此可以是与运行期页分配无关的**静态置换**（或仅依赖自身指令参数的置换），可移植、可复用；物理页的真正落点由 controller 在取指期统一算出。这就是「逻辑页/物理页」分层在这套设计里的价值。

---

## 5. 综合实践

把本讲三节串起来，完成一次「从取指到页序」的端到端追踪。

**任务**：给定一个 3 条指令的 opcode 序列（每条指令都是 PartialAttention，即 opcode 三次相同），手动排出第 2 条指令的完整 `pid_order`，并解释其中任意一个 `lid → pid` 映射背后完整的「取指 + 继承 + 回收同步」链路。

**建议步骤**：

1. **取指**：说明 controller 如何用 `load_instructions`（[instruction_fetch.cuh:11-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L11-L27)）把每条指令的 32 个 `int` 用一次合并事务读进对应 ring 槽位的 `instructions`。
2. **分页递推**：套用 4.2.2 的递推公式，列出 `pid_order_0`、`pid_order_1`、`pid_order_2`。PartialAttention 的 \(R(q) = (q+2) \bmod 13\)，于是每往前追一条指令，索引就「+2」一次。
   - `pid_order_0 = {0,1,2,3,4,5,6,7,8,9,10,11,12}`（基础恒等，**不**套用 `release_lid`）。
   - `pid_order_1[lid] = pid_order_0[(lid+2)%13] = (lid+2)%13` → `{2,3,4,5,6,7,8,9,10,11,12,0,1}`（旋转 2）。
   - `pid_order_2[lid] = pid_order_1[(lid+2)%13]` → `{4,5,6,7,8,9,10,11,12,0,1,2,3}`（旋转 4）。
   - **注意（易错点）**：虽然 opcode 出现了 3 次，但 `release_lid` 只被复合了 **2 次**。因为 `pid_order_0` 是基础情形、不经过任何 `release_lid`；而第 2 条指令的 `pid_order` 只依赖「第 0 条」和「第 1 条」的 `release_lid`——第 2 条自己的 opcode 决定的是 `pid_order_3`，而不是 `pid_order_2`。所以结果是旋转 4，不是旋转 6。
3. **解释一条链路**：以 `lid = 0` 为例，把继承链一路追回 `pid_order_0`：
   - 第 2 条指令的逻辑页 0：`pid_order_2[0] = pid_order_1[R(0)=2] = pid_order_1[2] = 4`，物理页号 = **4**；
   - 而 `pid_order_1[2] = pid_order_0[R(2)=4] = pid_order_0[4] = 4`；
   - 合起来：第 2 条指令的逻辑页 0 →（经 R）第 1 条指令的逻辑页 2 →（经 R）第 0 条指令的逻辑页 4 → 物理页 4。两次「+2」把逻辑页 0 最终落到物理页 4。
4. **回收同步**：说明为什么第 2 条指令的 worker 在真正用物理页 4 之前，必须先 `wait_page_ready(4)`（[util.cuh:155-161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L155-L161)）——因为物理页 4 此刻可能还握在第 1 条指令的 loader/consumer 手里（正是它把页 4 交接过来的），必须等它们 `finish_page` 之后才能接手。

**交付物**：一张三行的 `pid_order` 表 + 一段对 `lid=0` 继承链的文字解释。完成后，你应当能向别人讲清「controller 是如何在取指期就把整条流水里物理页的归属与交接排定的」。

## 6. 本讲小结

- **取指**：`load_instructions` 用「一 lane 一 int」的方式，把 32 个 `int`（128B）的指令以**一次合并全局内存事务**读进当前 ring 槽位的 `instructions`；`static_assert(INSTRUCTION_WIDTH <= 32)` 守住这个模型。
- **生产路径**：真正运行的是 [controller.cuh::main_loop](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 的 Step 1（取指）与 Step 2（分页）；`instruction_fetch.cuh` / `page_allocator.cuh` 里的 `_loop` 函数是等价的、未被实例化的参考实现。
- **页序递推**：\(\text{pid\_order}_n[\ell] = \text{pid\_order}_{n-1}[\text{release\_lid}_{n-1}(\ell)]\)，基础情形为恒等。物理页在指令之间**链式继承**。
- **为什么读上一条 opcode**：`release_lid` 属于「上一条指令那个 op」，因为释放页的是上一条；当前 opcode 决定的是「自己将来怎么释放」，不影响「自己怎么继承」。
- **`release_lid` 是契约**：每个 op 必须提供一个 `release_lid`，且它应是 \(\{0,\dots,P-1\}\) 上的**置换**，否则会出现两个逻辑页撞同一物理页的数据竞争。
- **动静结合**：`pid_order` 是取指期静态排定的交接计划，`page_finished` 信号量在运行期保证「上一家用完才轮到我」。

## 7. 下一步学习建议

- **紧接着读 [U6·L3]**：`semaphore_constructor` 与 `timings_store`。它们是 controller 四步里的后两步——构造本条指令动态需要的信号量集合、把计时数据回写 gmem。学完后你就完整掌握了 controller 的全部四步。
- **回顾 [U5·L3]**：如果你对 `pid_order` 存在哪、`pid(lid)` 怎么读、`wait_page_ready`/`finish_page` 怎么配合还有疑问，回去重读 vm-state-and-pages 的 4.2/4.3，会和本讲的递推公式严丝合缝地接上。
- **看一个完整的 op**：对照 [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) 的 `release_lid`（L70-L102）与它的 loader/consumer/storer，体会「`release_lid` 排定的页交接」是如何与三级 input/output 流水的收尾一一对应的（u8 流水讲义会专门讲）。
- **动手（可选）**：在一个最小 op（参考 `util/mk_init` 的 TestOp 模板，[util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L29)）里写一个自己的 `release_lid` 置换，思考它会如何改变下一条指令的 `pid_order`。
