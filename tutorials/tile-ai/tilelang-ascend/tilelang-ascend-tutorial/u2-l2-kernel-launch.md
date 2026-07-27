# kernel launch 与 T.Kernel（cid/vid/threads）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `with T.Kernel(blocks, is_npu=True)` 这个上下文到底做了什么：它如何把「数据切分后的 tile block」绑定到「Ascend 的逻辑核」。
- 区分两个关键返回值 `cid` 和 `vid`：`cid` 是「我负责哪个 tile」，`vid` 是「我是这个 tile 里的第几个 Vector 子核」。
- 理解 `VEC_NUM` 这个由用户自己定义的 Python 常量，以及为什么在「threads=None 模式」下，它必须和硬件的 Vector 子核数量（2）保持一致。
- 掌握 `threads=1/2` 这一套「vid 消除」写法：它如何让前端不再出现 `// VEC_NUM` 和 `vid * ...`，把子核切分交给编译器。

本讲只聚焦「kernel launch 这一刻发生的事」——也就是一个 tile block 如何被映射到核上。至于核内具体怎么搬运、怎么算（`T.copy`、`T.gemm_v0`、`T.alloc_*`），留给第三单元（Developer 模式核心原语）展开。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在 u1-l1、u1-l4、u2-l1 中已经建立）：

- **Ascend 的核与存储**：一个 Ascend AI Core 由一个 **Cube 核**（负责矩阵乘等密集计算）和若干 **Vector 核**（负责逐元素、reduce 等向量计算）组成。A2/A3 架构里 Cube 与 Vector 的配比（CV 配比）可以是 1:1 或 1:2，也就是「1 个 Cube 配 1 个或 2 个 Vector」。
- **数据切分**：由于片上存储（L1、UB）有限，一个大的 `(M, N)` 矩阵会被切成多个 `(block_M, block_N)` 大小的 **tile block**，每个 tile 由一个并发执行单元处理。tile 的总数通常写成 `m_num * n_num = (M//block_M) * (N//block_N)`。
- **TIR 与 `@T.prim_func`**：`T.Kernel` 是写在 `@T.prim_func` 函数体里的上下文管理器，它会被静态解析成 TensorIR（TIR）里的循环变量，而不是真正「执行」的代码。
- **JIT 与 `func.get_kernel_source()`**：被 `@tilelang.jit` 装饰的函数首次调用会触发编译，我们可以用 `func.get_kernel_source()` 把生成的 Ascend C 源码打印出来观察。

> 类比 GPU 读者：GPU 的 `T.Kernel` 返回 `blockIdx.x/y/z`，把 grid 切分到多个线程块。Ascend 这里的 `cid` 类似 `blockIdx.x`（tile 编号），而 `vid` 是 Ascend 独有的「Vector 子核编号」，GPU 上没有直接对应物。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/kernel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py) | Python 前端的 `Kernel()` 函数与 `KernelLaunchFrame`，决定 `T.Kernel(...)` 返回 `cid` 还是 `(cid, vid)`。 |
| [src/ir.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc) | C++ 后端的 `KernelLaunch`，真正创建 `cid`/`vid` 这两个 TIR 循环变量并绑定到 `blockIdx`/`threadIdx`。 |
| [src/transform/common/attr.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/attr.h) | 定义 `cv_1_1` / `cv_1_2` 两个 CV 配比常量字符串。 |
| [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) | 「threads=None、返回 (cid, vid)」的标准示例，手动用 `VEC_NUM` 切分。 |
| [examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py) | 「threads=2、只返回 cid」的示例，不出现 `// VEC_NUM`。 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 官方编程手册的 3.3 kernel launch 与 4.1.5.1 Vid 消除章节。 |
| [docs/tutorials/vid_reduction_and_auto_cv_ratio.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/vid_reduction_and_auto_cv_ratio.md) | Vid 消除与自动 CV 配比特性的官方说明。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 kernel launch 的本质**：`T.Kernel` 上下文如何把 tile block 绑定到逻辑核，`cid` 的含义。
2. **4.2 vid 与 VEC_NUM**：threads=None 模式下，用户如何手动表达「双 Vector 子核切分」。
3. **4.3 threads=1/2 与 vid 消除**：另一种写法，把切分交给编译器，前端只拿 `cid`。

### 4.1 kernel launch 的本质：把 tile block 绑定到逻辑核（cid）

#### 4.1.1 概念说明

回想 u1-l4 的 GEMM 示例：我们有一个 `(M, N)` 的输出矩阵，切成 `m_num * n_num` 个 tile。那么「谁来处理第几个 tile」？答案就是 `T.Kernel` 上下文。

`with T.Kernel(blocks, is_npu=True)` 做的事情可以理解为：**为切分后的每一个 tile 创建一个并发执行单元**。这个上下文管理器会返回一个或几个 TIR 变量，告诉当前这段代码「我是第几号执行单元」。在 Ascend 上，这个编号就叫 **`cid`**（cube/core id）。

- `cid` 的取值范围是 `[0, block_num)`，其中 `block_num = blocks` 就是传给 `T.Kernel` 的那个数（通常就是 `m_num * n_num`）。
- 每个 `cid` 对应一个 tile，运行时会被调度到某个物理 Cube 核上去执行。
- 因为 `cid` 是一个一维编号，而我们通常是二维的 tile 网格 `(m_num, n_num)`，所以需要用整数除法和取余把它还原成二维坐标：

```
bx = cid // n_num   # 当前 tile 在 M 方向的第几块
by = cid % n_num    # 当前 tile 在 N 方向的第几块
```

这就是你在几乎所有 Ascend 示例开头都会看到的「`cid → (bx, by)` 解码」。

#### 4.1.2 核心流程

整个 kernel launch 的流程（从 Python 到 TIR）：

```
@T.prim_func
def main(...):
    with T.Kernel(block_num, is_npu=True) as <返回值>:
        # 这里的代码会被复制 block_num 份，分别绑定到不同的 cid
        ...
```

1. Python 端调用 `T.Kernel(blocks, is_npu=True)`，进入 `Kernel()` 函数。
2. 根据 `is_npu` 和 `threads` 的取值，给本次 launch 打上一个 **属性标签**（`tilelang.is_npu_kernel_frame` 或 `tilelang.is_npu_kernel_frame_dev_mode`）。
3. 调用 C++ 的 `_ffi_api.KernelLaunch(...)`，在 TIR 里创建循环变量 `cid`（以及可能的 `vid`）。
4. `__enter__` 返回这些变量给 Python，于是 `with ... as (cid, vid)` 或 `with ... as (cid)` 就拿到了它们。
5. 函数体内对 `cid` 的所有运算（如 `cid // n_num`）都被记录进 TIR，编译后变成真实 kernel 里的地址计算。

#### 4.1.3 源码精读

先看 Python 入口 `Kernel()`。它对 NPU 有一个专门分支，断言「NPU kernel 只能有 1 维 grid」：

[tilelang/language/kernel.py:247-263](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L247-L263) —— 这段代码先断言 `len(blocks) == 1`，然后根据 `threads` 是否为 `None`，分别打上 `is_npu_kernel_frame`（threads 未指定）或 `is_npu_kernel_frame_dev_mode`（threads=1/2）两个不同的属性标签，最后调用 C++ 的 `_ffi_api.KernelLaunch(blocks, threads, attrs)`。

注意一个关键点：**属性标签不同，C++ 后端创建的循环变量就不同，Python 拿到的返回值也不同**。这正是 4.2 与 4.3 两种写法的分叉点。

再看 C++ 侧，`cid` 到底是怎么被创建出来的。`src/ir.cc` 的 `KernelLaunch` 函数里，`is_npu_kernel_frame` 分支（即 threads=None）做了两件事：

[src/ir.cc:249-259](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L249-L259) —— 创建 `cid`，绑定到 `blockIdx.x`，循环范围是 `grid_size[0]`（也就是你传入的 `m_num * n_num`）；同时创建 `vid`，绑定到 `blockIdx.y`。注意第 252-254 行的注释说清楚了 Ascend 上的对应关系：**`blockIdx.x` 对应 cube 核编号，`blockIdx.y` 对应 vec 核编号**。

这就是「cid = 我负责哪个 tile」在源码里的落点。

#### 4.1.4 代码实践：追踪 `cid → (bx, by)` 的解码

**实践目标**：把 `cid` 这个一维编号如何在示例里被解码成二维 tile 坐标看清楚。

**操作步骤**：

1. 打开 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)，定位第 31-33 行：

```python
with T.Kernel(m_num * n_num, is_npu=True) as (cid, vid):
    bx = cid // n_num
    by = cid % n_num
```

2. 用一组小数字手算：设 `M=4, N=4, block_M=2, block_N=2`，则 `m_num=2, n_num=2`，`m_num*n_num=4`。对 `cid = 0,1,2,3` 分别算出 `(bx, by)`，画一张 4 个 tile 在 `(bx, by)` 二维网格里的分布表。

**需要观察的现象**：每个 `cid` 唯一映射到一个 `(bx, by)`，4 个 `cid` 恰好覆盖 2×2 的全部 tile，没有遗漏也没有重叠。

**预期结果**：

| cid | bx = cid//n_num | by = cid%n_num | 对应输出矩阵行段 | 列段 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | [0,2) | [0,2) |
| 1 | 0 | 1 | [0,2) | [2,4) |
| 2 | 1 | 0 | [2,4) | [0,2) |
| 3 | 1 | 1 | [2,4) | [2,4) |

如果你想进一步用代码确认 `cid` 的取值范围，可以在算子跑通后加上 `print(func.get_kernel_source())`，在生成的 C++ 代码里找到形如 `for (... cid ...)` 的外层循环，它的上界就是 `m_num * n_num`。（这一步需要本地 Ascend 环境，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：如果输出矩阵是 `(M, N, P)` 三维，切分成 `(block_M, block_N, block_P)`，`cid` 仍然是一维的。请写出由 `cid` 解码出 `(bx, by, bz)` 的式子（假设 `m_num, n_num, p_num` 分别是三个方向的块数）。

**参考答案**：

```
bx = cid // (n_num * p_num)
by = (cid // p_num) % n_num
bz = cid % p_num
```

即「先除最后一维的乘积得到最高维，再用取余逐层剥落」。这正是把 1D 索引按行主序还原成多维坐标的标准方法。

---

### 4.2 vid 与 VEC_NUM：双 Vector 子核的手动切分

#### 4.2.1 概念说明

`cid` 解决了「哪个 tile」，但 A2/A3 上每个 Cube 核还配了 **2 个 Vector 核**（CV 配比 1:2）。也就是说，一个 tile 还可以再被 2 个 Vector 子核「对半劈开」并行处理，充分利用 Vector 计算资源。这第二个维度的编号就是 **`vid`**（vector id），取值 0 或 1。

于是在默认的 `threads=None` 写法里，`T.Kernel` 会同时返回 `(cid, vid)`：

- `cid`：范围 `[0, block_num)`，我负责哪个 tile；
- `vid`：范围 `{0, 1}`，我是这个 tile 里的第几个 Vector 子核。

**关键问题**：既然一个 tile 被 2 个 vid 对半分，那么每个 vid 实际只处理「半个 tile」。这要求前端代码必须**手动**在三个地方反映这种「对半」：

1. **UB 缓冲的形状**：要按 `// VEC_NUM` 缩小（每个子核只存自己那一半）。
2. **GM 地址偏移**：搬运时要加上 `vid * (block_M // VEC_NUM)`，让两个 vid 各自指向不同的半段。
3. **逐元素循环范围**：`T.Parallel` 的范围也要按 `// VEC_NUM` 缩小。

这里的 `VEC_NUM` **不是 `T.Kernel` 的参数**，而是你自己定义的一个 Python 常量，约定上等于 2（和硬件的 Vector 子核数一致）。它在源码里到处出现，本质是把「硬件有 2 个 Vector 子核」这个事实，手动翻译成前端代码。

> 这正是官方文档强调的一点：`VEC_NUM` 用来指定 vector 计算单元的数量；因为每个 AI Core 有两个 Vector 计算单元，所以每个切分后的 tile 还能再切成两个 sub tile。（见 [Programming Guide:155](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L155)）

#### 4.2.2 核心流程

threads=None 模式下，前端写一个 tile 内逐元素算子的「心智循环」是：

```
对每个 cid（一个 tile）:
    对每个 vid ∈ {0,1}（半个 tile）:        # 隐含的硬件并行
        申请 UB, shape 在切分轴上 // 2
        从 GM 的「vid 偏移位置」搬到 UB
        在 UB 上算（范围也 // 2）
        把结果搬回 GM 的「vid 偏移位置」
```

注意 `vid` 这个循环是**隐含**在硬件里的（你写的代码在每个 vid 上各跑一份），但「搬到哪、算多大、写回哪」这三个地址/大小问题，必须由你在前端用 `// VEC_NUM` 和 `vid * ...` 显式表达出来。一旦某处忘了除以 `VEC_NUM` 或忘了加 `vid` 偏移，两个子核就会算同一段或写错位置，得到错误结果。

#### 4.2.3 源码精读

先确认 C++ 给 `vid` 设的范围是固定的 2：

[src/ir.cc:258-259](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L258-L259) —— `vid` 被绑定到 `blockIdx.y`，且范围被**硬编码为 2**。这就是为什么前端的 `VEC_NUM` 必须取 2：它只是给这个硬编码的 2 起一个名字，方便你同步维护多处 `// 2` 和 `vid * ... // 2`。

再看一个完整的「手动切分」示例 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py)。三个手动切分点全部出现了：

- [elementwise_add.py:23](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L23)：`VEC_NUM = 2`，用户自定义的常量。
- [elementwise_add.py:35-37](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L35-L37)：UB 的形状是 `(block_M // VEC_NUM, block_N)`，第 0 维（M 方向）缩小一半。
- [elementwise_add.py:39-40](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L39-L40)：搬运时 GM 起点 `bx * block_M + vid * block_M // VEC_NUM`，用 `vid` 把两个子核指向不同的半段。
- [elementwise_add.py:46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L46)：写回 GM 时同样带 `vid` 偏移。

把这三处合起来看，整段 [elementwise_add.py:31-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L31-L46) 描述的就是「一个 tile 由 (cid, vid) 共同确定，vid 负责半个 tile」的完整图景。

#### 4.2.4 代码实践：数清「必须和 VEC_NUM 保持一致」的所有位置

**实践目标**：建立对「手动切分很啰嗦、且容易漏」的直观感受，为 4.3 的「vid 消除」做铺垫。

**操作步骤**：

1. 在 [elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 里，统计所有出现 `VEC_NUM` 或 `vid` 的行。
2. 对每一处，判断它属于「UB 形状」「GM 偏移」「循环范围」中的哪一类。
3. 思考：如果把 `VEC_NUM` 改成 1（但 `threads` 仍为 `None`，即 C++ 里 `vid` 范围仍是硬编码的 2），哪些假设会被打破？

**需要观察的现象**：你会发现 `VEC_NUM` 在这个短小的 kernel 里至少出现了 6 次以上（3 个 UB 各 1 次 + 3 个搬运偏移），每一处都得和「vid 范围是 2」对齐。

**预期结果（推理型，待本地验证）**：如果 `VEC_NUM=1` 但 `vid` 实际范围仍是 2，那么两个 vid 会按 `vid * block_M // 1` 计算 GM 偏移，即 vid=0 写 `[bx*block_M, bx*block_M+block_M)`，vid=1 写 `[bx*block_M+block_M, ...)`——两个子核写到了相邻 tile 的区域，结果互相覆盖，输出错误。结论：**`VEC_NUM` 必须严格等于硬件 vid 范围（2）**，这正是手动写法的脆弱之处。

#### 4.2.5 小练习与答案

**练习 1**：在 elementwise_add.py 中，为什么 UB 是按「M 方向」对半切（`block_M // VEC_NUM`），而不是按 N 方向切？

**参考答案**：切分方向并没有硬性规定必须沿 M，elementwise_add.py 只是**约定**沿第 0 维切。理论上沿 N 方向切（`block_N // VEC_NUM`，偏移加在 `by * block_N + vid * block_N // VEC_NUM`）同样正确，只要 UB 形状、GM 偏移、循环范围三处的切分轴保持一致即可。关键是「三处必须一致」，而不是「必须是 M」。

**练习 2**：`vid` 的取值为什么只有 0 和 1，而不能是 0、1、2、3？

**参考答案**：因为 A2/A3 每个 Cube 核只配 1 个或 2 个 Vector 子核，C++ 里把 `vid` 范围硬编码成了 2（[src/ir.cc:258-259](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L258-L259)）。`vid` 是「物理 Vector 子核编号」，数量由硬件决定，不可由前端随意指定。

---

### 4.3 threads=1/2 与 vid 消除：把子核切分交给编译器

#### 4.3.1 概念说明

4.2 的手动写法把硬件细节（2 个 Vector 子核）暴露在了前端，每写一个算子都要维护一堆 `// VEC_NUM` 和 `vid * ...`，既啰嗦又容易出错。TileLang 提供了另一种写法来屏蔽这些细节：**给 `T.Kernel` 传 `threads=1` 或 `threads=2`**。

这种写法下：

1. `T.Kernel(..., threads=2, is_npu=True) as (cid)`——**返回值里只剩 `cid`，没有 `vid`**。
2. 前端代码写**完整形状**的 UB（`block_M` 不再 `// 2`）、写**不带 `vid` 偏移**的 GM 地址，就像「每个 tile 只有一个 Vector 核」一样。
3. 编译器的 **AscendVidReduction** pass 会根据 CV 配比，自动帮你做 4.2 里那些 `// VEC_NUM` 切分和 `vid` 偏移。

`threads` 的取值同时决定了**硬件 CV 配比**：

- `threads=1`：C:V = 1:1（每个 Cube 配 1 个 Vector）。
- `threads=2`：C:V = 1:2（每个 Cube 配 2 个 Vector）。

> 官方原文：参数 `threads` 必须被设置（只允许 1 或 2），当设置了 `threads` 参数，返回值只能包含 `cid`，不能有 `vid`。（见 [Programming Guide:1206](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1206)）

一句话总结两种模式的关系：**它们表达的是同一个硬件事实（1 Cube + 2 Vector），区别只在「谁来写 `// 2`」**。threads=None 时由人写（vid 可见），threads=2 时由编译器写（vid 被消除）。

#### 4.3.2 核心流程

threads=1/2 模式的 launch 流程：

1. Python `Kernel()` 检测到 `threads` 为 1 或 2，打上 `is_npu_kernel_frame_dev_mode` 标签（[kernel.py:262](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L262)）。
2. C++ `KernelLaunch` 在这个分支里**仍然会创建 `vid`**（绑定到 `threadIdx.x`），并把它写进 TIR——但前端 `__enter__` 只把 `cid` 返回给你，所以你在 Python 里看不到 `vid`（[src/ir.cc:269-271](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L269-L271)）。
3. C++ 还会根据 `threads` 给整个 PrimFunc 打上 CV 配比属性：`threads=1` → `cv_1_1`，`threads=2` → `cv_1_2`（[src/ir.cc:282-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L282-L287)）。
4. 后续的 AscendVidReduction pass 读到这个 CV 配比，自动把完整形状的 UB 拆成两半、自动在 GM 地址上插入 `vid * ...` 偏移。

也就是说，vid 在「TIR 层」其实一直存在，只是 dev 模式下对**前端用户**不可见，切分工作被挪到了编译 pass 里。这也是为什么 4.3 的写法能让你写「看起来只有一个 Vector 核」的代码。

#### 4.3.3 源码精读

先看 Python 端 `__enter__` 的返回值差异。[tilelang/language/kernel.py:101-104](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/kernel.py#L101-L104) —— `maybe_npu`（threads=None）返回前两个 frame 的变量（即 `(cid, vid)`）；而 `maybe_npu_dev_mode`（threads=1/2）只返回 `frames[0]`，也就是 `cid`。这正是「返回值只剩 cid」在源码里的落点。

再看 C++ 的 dev_mode 分支，它做了两件 threads=None 分支没有的事：

[src/ir.cc:260-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L260-L287) —— 这里 `cid` 仍绑定 `blockIdx.x`，但 `vid` 改为绑定到 `threadIdx.x`，范围取自 `block_size[0]`（也就是你传的 `threads`）；随后把 `threads` 的值（1 或 2）转成 `npu_cv_ratio` 属性写进 PrimFunc。这两个常量字符串定义在 [src/transform/common/attr.h:23-25](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/attr.h#L23-L25)：`cv_1_1` 表示 1:1，`cv_1_2` 表示 1:2。

最后看一个完整的「vid 消除」示例 [examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py)。对比 4.2 的 elementwise_add.py，关键差异一目了然：

- [matmul_add_developer.py:40](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L40)：`with T.Kernel(m_num * n_num, threads=2, is_npu=True) as (cid):`——多了 `threads=2`，返回值只有 `cid`。
- [matmul_add_developer.py:48-49](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L48-L49)：UB 形状是完整的 `(block_M, block_N)`，**没有任何 `// VEC_NUM`**。
- [matmul_add_developer.py:53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L53) 与 [matmul_add_developer.py:66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L66)：GM 偏移是 `bx * block_M`，**没有任何 `vid * ...`**。

整段 [matmul_add_developer.py:40-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L40-L66) 通篇找不到 `vid` 和 `VEC_NUM`，但它实际上仍然跑在 1:2 的双 Vector 配比上——切分全由编译器代劳。

#### 4.3.4 代码实践：把 elementwise_add.py 改写成 threads=2 形式

**实践目标**：亲手把一个「(cid, vid) 手动切分」的算子，改写成「只返回 cid、vid 消除」的形式，并验证结果不变。

**操作步骤**：

1. 复制 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 为 `elementwise_add_vidreduce.py`。
2. 把 kernel 上下文改成只返回 cid：

```python
# 示例代码（基于 elementwise_add.py 改写）
with T.Kernel(m_num * n_num, threads=2, is_npu=True) as (cid):
    bx = cid // n_num
    by = cid % n_num

    # UB 改成完整形状，去掉 // VEC_NUM
    a_ub = T.alloc_ub((block_M, block_N), dtype)
    b_ub = T.alloc_ub((block_M, block_N), dtype)
    c_ub = T.alloc_ub((block_M, block_N), dtype)
    with T.Scope("V"):
        # GM 偏移去掉 + vid * block_M // VEC_NUM
        T.copy(A[bx * block_M, by * block_N], a_ub)
        T.copy(B[bx * block_M, by * block_N], b_ub)

        T.barrier_all()
        T.tile.add(c_ub, a_ub, b_ub)
        T.barrier_all()

        T.copy(c_ub, C[bx * block_M, by * block_N])
```

3. 删去脚本里 `VEC_NUM = 2` 这一行（它已不再被使用）。
4. 运行 `python elementwise_add_vidreduce.py`。

**需要观察的现象**：

- `T.Kernel` 的返回值由 `(cid, vid)` 变成了单个 `cid`。
- kernel 体内不再出现 `VEC_NUM` 和 `vid`，UB 是完整 `(block_M, block_N)`，GM 偏移只剩 `bx * block_M`。

**预期结果**：脚本应打印 `init successful!` 和 `Kernel Output Match!`，与原版结果一致（因为两种写法表达的是同一个 1:2 双 Vector 切分，只是切分由谁来做不同）。这一步需要本地 Ascend 环境运行，**待本地验证**。

**进阶观察**：在改写后的脚本末尾加一行 `print(func.get_kernel_source())`，在生成的 C++ 代码里搜索由 AscendVidReduction 自动插入的子核切分痕迹（例如 UB 上按 vid 拆分的循环或地址计算），对比你在 4.2 手写时需要自己维护的那些 `// 2`。这样能直观看到「vid 消除」到底消除了什么。（需要本地环境，**待本地验证**。）

#### 4.3.5 小练习与答案

**练习 1**：threads=None 模式（返回 (cid, vid)）和 threads=2 模式（返回 cid）最终生成的硬件行为是否相同？为什么？

**参考答案**：相同。两者都对应 C:V=1:2、每个 tile 由 2 个 Vector 子核各处理一半。区别仅在于：threads=None 时切分（`// 2`、`vid * ...`）由前端用户手写、`vid` 可见；threads=2 时切分由 AscendVidReduction pass 根据 `npu_cv_ratio=cv_1_2` 自动完成、`vid` 对用户不可见。C++ 后端在两种模式下都会创建 `vid` 这个 TIR 变量，只是 dev 模式不把它返回给 Python。

**练习 2**：如果你想写一个只用 1 个 Vector 子核（C:V=1:1）的算子，应该用哪种 launch 写法？UB 形状该怎么写？

**参考答案**：用 `with T.Kernel(block_num, threads=1, is_npu=True) as (cid):`。此时 CV 配比为 1:1，AscendVidReduction 不会做对半切分，UB 直接写完整形状 `(block_M, block_N)`、GM 偏移写 `bx * block_M` 即可——和 threads=2 的前端写法几乎一样，只是 `threads` 传 1。

**练习 3**：能否在同一个 kernel 里，上半段用 threads=None（拿 vid），下半段用 threads=2（拿 cid）？

**参考答案**：不能。`threads` 是 `T.Kernel` 这一个上下文的属性，决定了整个 launch 的模式，必须二选一。一个 kernel 只有一个 `T.Kernel` 上下文，也就只有一种 vid 处理方式。

---

## 5. 综合实践

**任务**：写一个二维 elementwise 加法算子 `C = A + B`，**同时**实现 threads=None 和 threads=2 两个版本，运行后验证两者输出一致，并通过源码对比理解「vid 可见 vs vid 消除」。

**参考实现骨架**（基于本讲示例改写，标注为「示例代码」）：

```python
# 示例代码
import tilelang
import tilelang.language as T
import torch


def make_vec_add(M, N, block_M, block_N, *, mode, dtype="float"):
    """mode='manual' 表示 threads=None 手动切分；mode='vidreduce' 表示 threads=2。"""
    m_num = M // block_M
    n_num = N // block_N

    @T.prim_func
    def main(A: T.Tensor((M, N), dtype), B: T.Tensor((M, N), dtype),
             C: T.Tensor((M, N), dtype)):
        if mode == "manual":
            VEC_NUM = 2  # 仅在 manual 分支里使用
            with T.Kernel(m_num * n_num, is_npu=True) as (cid, vid):
                bx = cid // n_num
                by = cid % n_num
                a_ub = T.alloc_ub((block_M // VEC_NUM, block_N), dtype)
                b_ub = T.alloc_ub((block_M // VEC_NUM, block_N), dtype)
                c_ub = T.alloc_ub((block_M // VEC_NUM, block_N), dtype)
                with T.Scope("V"):
                    off = bx * block_M + vid * block_M // VEC_NUM
                    T.copy(A[off, by * block_N], a_ub)
                    T.copy(B[off, by * block_N], b_ub)
                    T.barrier_all()
                    T.tile.add(c_ub, a_ub, b_ub)
                    T.barrier_all()
                    T.copy(c_ub, C[off, by * block_N])
        else:  # vidreduce
            with T.Kernel(m_num * n_num, threads=2, is_npu=True) as (cid):
                bx = cid // n_num
                by = cid % n_num
                a_ub = T.alloc_ub((block_M, block_N), dtype)
                b_ub = T.alloc_ub((block_M, block_N), dtype)
                c_ub = T.alloc_ub((block_M, block_N), dtype)
                with T.Scope("V"):
                    T.copy(A[bx * block_M, by * block_N], a_ub)
                    T.copy(B[bx * block_M, by * block_N], b_ub)
                    T.barrier_all()
                    T.tile.add(c_ub, a_ub, b_ub)
                    T.barrier_all()
                    T.copy(c_ub, C[bx * block_M, by * block_N])

    return main


# 用法（需要本地 Ascend 环境，待本地验证）：
# f_manual = tilelang.jit(out_idx=[-1])(make_vec_add)(1024, 1024, 128, 256, mode="manual")
# f_reduce = tilelang.jit(out_idx=[-1])(make_vec_add)(1024, 1024, 128, 256, mode="vidreduce")
# a = torch.randn(1024, 1024).npu(); b = torch.randn(1024, 1024).npu()
# torch.testing.assert_close(f_manual(a, b), f_reduce(a, b), rtol=1e-2, atol=1e-2)
# print("Both modes match!")
```

> 注意：上面 `if mode == "manual"` 是普通 Python 分支，`mode` 是编译期常量，会被折叠——只有选中的那个分支进入 TIR。这种「用 Python 常量在编译期选择 kernel 结构」的写法在 TileLang 里很常见（参见 u2-l3 关于「Python 常量折叠」的说明）。

**需要观察/记录的现象**：

1. 两个版本都能跑出 `Kernel Output Match!`（或上面骨架里的 `Both modes match!`）。
2. `manual` 版本里 `VEC_NUM`/`vid` 出现了多少次；`vidreduce` 版本里它们一次都没出现。
3. 用 `func.get_kernel_source()` 对比两个版本生成的 C++，确认 `vidreduce` 版本里那些 `// 2` 切分是被编译器（AscendVidReduction pass）补回去的。

**预期结果**：两版输出数值一致；`vidreduce` 版前端代码明显更简洁。这一步需要本地 Ascend 环境运行，**待本地验证**。

## 6. 本讲小结

- `with T.Kernel(block_num, is_npu=True)` 为每个 tile block 创建一个并发执行单元；`cid ∈ [0, block_num)` 表示「我负责哪个 tile」，通常用 `bx = cid // n_num; by = cid % n_num` 解码成二维坐标。
- Ascend 每个 Cube 核配 1~2 个 Vector 子核，第二个维度编号 `vid ∈ {0,1}` 表示「我是这个 tile 里的第几个 Vector 子核」；C++ 里 `cid` 绑定 `blockIdx.x`、`vid` 绑定 `blockIdx.y`（threads=None 时）。
- 默认的 `threads=None` 写法返回 `(cid, vid)`，要求用户用自定义常量 `VEC_NUM=2` 手动维护 UB 形状（`// 2`）、GM 偏移（`+ vid * ... // 2`）和循环范围三处切分。
- `threads=1/2` 写法只返回 `cid`，前端写完整形状、不带 `vid` 偏移，由 AscendVidReduction pass 根据 `npu_cv_ratio`（`cv_1_1`/`cv_1_2`）自动完成切分，屏蔽了硬件细节。
- `threads` 同时声明硬件 CV 配比：`threads=1` → C:V=1:1，`threads=2` → C:V=1:2；两种写法表达的硬件行为一致，差别只在「切分由人写还是编译器写」。
- `vid` 在 TIR 层始终存在（dev 模式绑定到 `threadIdx.x`），只是 dev 模式下不返回给 Python 用户。

## 7. 下一步学习建议

- **继续本单元（u2）**：下一讲 u2-l3「循环与控制流原语」会讲 `T.serial`/`T.unroll`/`T.Parallel`/`T.Pipelined`/`T.Persistent` 等，其中 `T.Parallel` 的范围经常会和本讲的 `// VEC_NUM` 配合使用（回顾 [elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 里 `T.tile.add` 与 Programming Guide 里的 `T.Parallel(block_M // VEC_NUM, block_N)` 写法）。
- **进入第三单元（u3）**：本讲只解决了「tile 如何绑定到核」，核内具体怎么分配 UB/L1/L0（`T.alloc_ub`/`alloc_shared`/`alloc_fragment`）、怎么搬运（`T.copy`）、怎么算（`T.gemm_v0`），都在 u3 的 Developer 模式核心原语里展开。
- **关于 vid 消除的底层**：如果想深入 AscendVidReduction pass 如何自动插入切分，可跳到 u5-l3「Vid 消除与自动 CV 配比」，那里会逐行读 [src/transform/ascend_vid_reduction.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_vid_reduction.cc)。
- **建议阅读的源码**：把 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 和 [examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py) 并排打开对照，是巩固本讲「两种 launch 模式」最直接的方式。
