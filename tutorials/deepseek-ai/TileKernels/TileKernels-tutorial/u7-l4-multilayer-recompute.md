# 多层重计算 kernel

## 1. 本讲目标

本讲围绕 mhc（Manifold HyperConnection，流形超连接）模块里一个「跨层融合」的特殊算子 `mhc_multilayer_recompute` 展开。学完本讲你应该能够：

- 说清 **梯度检查点 / 重计算（rematerialization）** 的动机，以及 mhc 训练时为什么需要把多层 pre+post 链一次性重算。
- 读懂 `multilayer_recompute_kernel.py`：如何用一个 TileLang kernel 把 L 层的 `layer_input`（pre 段）与 `residual`（post 段）全部算出，且让中间残差始终留在片上。
- 理解「指针表 + `T.make_tensor`」机制如何让一份编译产物寻址运行时可变的张量列表。
- 厘清 `multilayer_recompute` 在 `modeling/mhc/ops` 层的真实封装形态：它**没有** `torch.autograd.Function` 包装，与 `pre_big_fuse` 同属「无反向」原语，但原因不同。
- 对比 mhc 的两条路径：**训练（拆分 + 重算）** 与 **推理（big_fuse 融合）**，解释各自的取舍。

## 2. 前置知识

在进入本讲前，请先确认你理解以下概念（对应前置讲义 u7-l1 ~ u7-l3）：

- **mhc 残差扩展**：标准 Transformer 的单股残差 `(..., H)` 被升级为 `mhc_mult` 股并行残差 `(..., mhc_mult, H)`，当前实现仅 `mhc_mult=4` 可用（见 u7-l1）。
- **pre 与 post**：`mhc_pre` 把多股残差加权压成单股 `layer_input` 喂给子层；`mhc_post` 把子层输出 `layer_output` 展开混回多股残差。两段的数学是：
  - pre：\( \text{layer\_input}[h] = \sum_{m} \text{pre\_mix}[m] \cdot \text{res}[m, h] \)
  - post：\( \text{new\_res}[m_o, h] = \text{post\_mix}[m_o] \cdot \text{layer\_output}[h] + \sum_{m_i} \text{comb\_mix}[m_i, m_o] \cdot \text{res}[m_i, h] \)
- **`ctx=(post_mix, comb_mix)`**：`mhc_pre` 把 post 段需要的混合系数打包成不透明元组交给 `mhc_post`（见 u7-l1、u7-l2）。
- **训练 vs 推理分流**：`mhc_pre` 用 `torch.is_grad_enabled()` 在推理态走 `pre_big_fuse` 四合一融合 kernel、训练态走四步拆分（见 u7-l2）。
- **重计算（rematerialization）/ 激活检查点**：训练时为省显存，前向不保存中间激活，反向需要时再从保存的输入重算一遍。本讲的算子正是为这种场景准备的。

一个关键直觉：pre 和 post 都是**带宽受限（bandwidth-bound）**算子——每 token 搬运的残差字节数远多于算的乘加。因此「少跑一次 HBM 往返」就是实打实的加速。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/mhc/multilayer_recompute_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py) | 底层 TileLang kernel：把 L 层 pre+post 链融合进单个 kernel，含指针表构造与 wrapper。本讲主角。 |
| [tile_kernels/modeling/mhc/ops/multilayer_recompute.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/multilayer_recompute.py) | ops 层封装：**目前只是纯 re-export**，没有 autograd.Function。 |
| [tile_kernels/modeling/mhc/ops/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py) | ops 层聚合出口，把 `mhc_multilayer_recompute` 与其它 op 一起导出。 |
| [tile_kernels/modeling/mhc/functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py) | `mhc_pre` 在此按 `is_grad_enabled()` 分流（推理 big_fuse / 训练拆分），是讨论训练-推理两条路径的参照点。 |
| [tile_kernels/mhc/post_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py) | 单层 post 的前向/反向 kernel，重算 kernel 的 post 段与之数值等价。 |
| [tile_kernels/mhc/pre_apply_mix_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py) | 单层 pre 的前向/反向 kernel，重算 kernel 的 pre 段与之数值等价。 |
| [tests/mhc/test_multilayer_recompute.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_multilayer_recompute.py) | 正确性对拍（与逐层 pre_apply_mix+post 位精确）与 benchmark（含理论带宽比 `theory`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 跨层融合重算 kernel（multilayer_recompute_kernel）

#### 4.1.1 概念说明

**问题来源：训练时的显存压力。** 一个 mhc Transformer 有很多层，每层都有一套 mhc 残差流。如果前向把每层的中间激活（`layer_input`、post 前后的 `residual`）都存下来留给反向，显存会随层数线性增长。常见解法是**梯度检查点**：前向只存少量「检查点」（比如每层的输入残差、混合系数、子层输出），反向时把这些中间量**重算**出来。

**朴素重算的开销。** 若反向时逐层、独立地调用 `mhc_pre_apply_mix`（算 `layer_input`）与 `mhc_post`（算新残差），那么相邻两层之间的中间残差会被反复读写 HBM：

- `post i` 把新残差 `residual[i]` **写**回 HBM；
- `pre (i+1)` 又把它**读**回来；
- `post (i+1)` 还要把它当输入**再读**一次。

这些中间残差形状是 `(n, mhc_mult, hidden)` 的 bf16 张量，体积大，反复搬运就是带宽浪费。

**融合的核心思想。** `mhc_multilayer_recompute` 把整条 L 层的 pre+post 链装进**单个 kernel**，让中间残差 `res_local` 始终驻留在片上（寄存器/shared memory），只在每层结束、必须交给外部使用时（写出 `layer_input[i]` 和 `residual[i]`）才碰一次 HBM。它的数学与「逐层 pre_apply_mix + post」完全等价，但省掉了层间残差的 HBM 往返——这是一次**纯带宽优化**，不改语义。

> 一句话：`multilayer_recompute` = 「从 `initial_residual` 出发，沿 mhc 层链滚动重算，残差不出片」。

#### 4.1.2 核心流程

先看 wrapper 暴露的输入输出与约束（[multilayer_recompute_kernel.py:154-170](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L154-L170)）：

- **输入**：`initial_residual (n, mhc_mult, hidden)`，外加四个**输入 list**：`pre_mix_list`、`layer_output_list`、`post_mix_list`、`comb_mix_list`。
- **输出（就地写入空 list）**：`layer_input_list`（每层的 `layer_input`）、`residual_list`（每层 post 产生的新残差）。
- **层数约束**：`num_post == num_layers` 或 `num_post == num_layers - 1`。

为什么允许 `num_post == num_layers - 1`？因为链上**最后一层可能只有 pre 没有 post**——它对应 lm_head 前的 pre（只需算 `layer_input`，不需要再产生新残差）。其余层都是「pre + post」成对出现。

单 block（处理一个 token）的执行流程如下：

```
载入 initial_residual → res_local（片上）
若 L_post > 0：异步预取第 0 层的 (layer_output, pre_mix, post_mix, comb_mix) 到 shared 双缓冲[0]
for i_layer in [0, L_post):                       # 串行，轮间有数据依赖
    phase = i_layer % 2
    若还有下一层：异步预取下一层数据到 shared 双缓冲[1-phase]   # 软件流水线
    ptx_wait_group：等当前层数据到位
    # —— pre 段 ——
    layer_input[h] = Σ_m pre_mix[m] * res_local[m, h]      # 写出 layer_input[i_layer]
    # —— post 段 ——
    new_res[m_o,h] = post_mix[m_o]*layer_output[h]
                   + Σ_{m_i} comb_mix[m_i,m_o] * res_local[m_i,h]
    写出 residual[i_layer] = new_res
    res_local = fp32(bf16(new_res))              # 关键：bf16 往返，保证与逐层参考位精确一致
若 num_layers > num_post：                       # 尾部 pre-only 层
    layer_input_last[h] = Σ_m pre_mix_last[m] * res_local[m, h]
```

几个要点先建立直觉：

1. **网格是 `(n,)` 一维**：每个 block 处理一个 token，hidden 维度按 `h_blk` 分块在 `T.serial` 里串行（[L68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L68)、[L82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L82)）。
2. **`res_local` 跨层常驻**：它在层循环外分配（[L69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L69)），每层只更新内容、不重新分配，这正是「残差不出片」的来源。
3. **双缓冲异步预取**：每层的四份数据用 `T.async_copy` 提前搬进 shared，用 `T.ptx_wait_group` 同步，让「搬下一层」与「算当前层」重叠。
4. **bf16 往返保证位精确**：`res_local` 在每层 post 后被强制 round-trip 到 bf16 再回 fp32（[L135-L136](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L135-L136)），与逐层参考（残差以 bf16 在层间落盘）完全对齐，因此测试用 `torch.equal` 做位精确对拍。

#### 4.1.3 源码精读

**(a) 编译开关与 kernel 构造器。** kernel 用三个 `pass_configs` 关掉 warp specialize、提高 ptxas 寄存器预算、禁用 256 位向量化（[L8-L12](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L8-L12)）。构造器参数 `mhc_mult / hidden / num_layers / num_post` 都是**编译期**参数（被烤进产物），`num_tokens` 才是运行时符号（[L41-L54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L41-L54)）。这意味着**不同层数会各自特化出一份编译产物**。

**(b) 指针表：让一份产物寻址可变张量列表。** 这是本 kernel 最值得学的一个技巧。prim_func 的形参里，各层的 `pre_mix / layer_output / post_mix / comb_mix / layer_input / residual` 都不是一个个独立张量，而是**一维指针数组**（[L58-L67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L58-L67)）：

```python
pre_mix_ptrs:      T.Tensor[(L,), T.ptr],         # L 个 pre_mix 张量的基地址
layer_output_ptrs: T.Tensor[(L_post,), T.ptr],
...
```

在循环体里，用 `T.make_tensor(ptrs[i_layer], (n, h), T.bfloat16)` 把第 `i_layer` 个原始指针「物化」成一个有形状的张量，再正常索引（[L96-L97](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L96-L97)）。这样编译期只需要知道「有 L 层」，运行时每层具体指向哪块显存由指针数组决定。

指针数组由 wrapper 侧的 `_make_ptr_tables_batched` 构造（[L15-L38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L15-L38)）：把每个张量的 `data_ptr()` 收集到 pin memory 的 CPU buffer，再 `non_blocking` 拷到 GPU，按 list 切出多个 view。

**(c) 片上缓冲分配。** 注意哪些是 fragment（寄存器）、哪些是 shared（[L69-L80](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L69-L80)）：

- `res_local / new_res_local (mhc, h_blk)`：跨层残差，fragment。
- `layer_input_local / layer_output_local (h_blk,)`：单层 pre/post 的 hidden 向量，fragment。
- `*_shared (2, ...)`：四类输入的**双缓冲**，第一维 `2` 就是双缓冲的两槽。

**(d) 双缓冲异步预取与同步。** 进层循环前先预取第 0 层到 `shared[0, :]`（[L85-L93](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L85-L93)）。层循环里，若存在下一层，就把下一层数据 `async_copy` 到 `shared[1-phase, :]`（另一槽），随后用 `T.ptx_wait_group` 同步（[L101-L114](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L101-L114)）：

```python
if i_layer + 1 < L_post:
    # 预取「下一层」到另一槽
    T.async_copy(next_layer_output_tensor[i_n, i0_h*h_blk], layer_output_shared[1-phase, :])
    ...（共 4 个 async_copy：layer_output / pre_mix / post_mix / comb_mix）
    T.ptx_wait_group(4)     # 等到 in-flight 的 async_copy ≤ 4，即「当前层」的 4 个预取完成
else:
    T.ptx_wait_group(0)     # 最后一层：等全部完成
```

直觉理解 `ptx_wait_group(4)`：每层正好 4 个 `async_copy`。迭代 `i` 先发出「下一层」的 4 个，此时 in-flight 共 8 个；`wait_group(4)` 等到只剩 4 个，即完成了最早的 4 个——也就是「当前层」的数据。最后一层不再发新预取，用 `wait_group(0)` 把残留的全部等齐。这是经典的 double-buffer 软件流水线。

**(e) pre 段。** 读 `pre_mix`，按 mhc 串行、hidden 并行累加（[L116-L123](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L116-L123)）：

```python
T.clear(layer_input_local)
for i_mhc in T.serial(mhc):
    for i1_h in T.Parallel(h_blk):
        layer_input_local[i1_h] += pre_mix_local[i_mhc] * res_local[i_mhc, i1_h]
T.copy(layer_input_local, layer_input_tensor[i_n, i0_h*h_blk])
```

这与单层 [pre_apply_mix_kernel.py:47-49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L47-L49) 数学一致，只是残差来自片上 `res_local` 而非 HBM。

**(f) post 段与 bf16 往返。** 读 `post_mix / comb_mix / layer_output`，按 \( \text{new\_res}[m_o,h] = \text{post\_mix}[m_o]\cdot\text{layer\_output}[h] + \sum_{m_i}\text{comb\_mix}[m_i,m_o]\cdot\text{res}[m_i,h] \) 计算（[L125-L136](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L125-L136)）：

```python
for i_mhco, i1_h in T.Parallel(mhc, h_blk):
    new_res_local[i_mhco, i1_h] = post_mix_local[i_mhco] * layer_output_local[i1_h]
    for i_mhci in T.serial(mhc):
        new_res_local[i_mhco, i1_h] += comb_mix_local[i_mhci, i_mhco] * res_local[i_mhci, i1_h]
T.copy(new_res_local, output_residual_tensor[i_n, 0, i0_h*h_blk])
# 关键：把残差 round-trip 到 bf16，与逐层参考的「层间落盘 bf16」对齐
for i_mhc, i1_h in T.Parallel(mhc, h_blk):
    res_local[i_mhc, i1_h] = T.cast(T.cast(new_res_local[i_mhc, i1_h], T.bfloat16), T.float32)
```

与单层 [post_kernel.py:50-53](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py#L50-L53) 的 `c*d + a@b` 完全同构（`a=comb_mix`、`b=residual`、`c=post_mix`、`d=layer_output`）。

> **为什么必须做 bf16 往返？** 逐层参考实现里，`mhc_post` 输出的残差是 bf16 张量（`residual_list` 的 dtype），下一层 `mhc_pre_apply_mix` 读到的就是这个 bf16 值。融合 kernel 把残差留在 fp32 片上，若不主动 round-trip 到 bf16，累积的舍入差异会让结果与参考**不位精确**。这一行是「融合但不改语义」的关键保险。

**(g) 尾部 pre-only 层。** 当 `num_layers > num_post`，最后一层（下标 `L_post`）只做 pre、不做 post（[L138-L149](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L138-L149)），对应 lm_head 前的那次 pre。

**(h) wrapper。** 校验形状与层数约束、构造六张指针表、触发 JIT 编译并启动（[L163-L196](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L163-L196)）。注意 `initial_residual.view(-1, mhc_mult, hidden)`：wrapper 把任意前导维度摊平成 `n`，与 kernel 的 `(n, mhc, h)` 形参对齐。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用测试里的逐层参考，验证「融合 kernel 的 pre/post 两段就是 pre_apply_mix + post 的链式调用」，并理解其位精确性。

**操作步骤**：

1. 打开 [tests/mhc/test_multilayer_recompute.py:60-76](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_multilayer_recompute.py#L60-L76)，阅读 `_mhc_multilayer_recompute_ref`：它从 `initial_residual` 出发，逐层 `mhc_pre_apply_mix(residual, pre_mix)` 算 `layer_input`，再 `mhc_post(layer_output, residual, post_mix, comb_mix)` 算新 `residual`。
2. 对照本讲 4.1.3 的 (e)(f)，确认两条路径的数学一致。
3. （可选）在有 GPU 与正确依赖的环境运行正确性测试：
   ```bash
   pytest tests/mhc/test_multilayer_recompute.py -k correctness -n 4
   ```

**需要观察的现象**：

- 正确性断言用的是 `torch.equal`（位精确，非浮点容差），见 [L101-L107](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_multilayer_recompute.py#L101-L107)。这说明融合 kernel 与逐层参考**逐字节相同**。

**预期结果**：

- 若环境满足，测试通过；若无法运行，明确标注「待本地验证」，但你能从源码推出：位精确性由两点保证——pre/post 数学同构 + `res_local` 的 bf16 往返。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `res_local` 的 bf16 往返（[L135-L136](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py#L135-L136)）不能省？如果删掉它会怎样？

**参考答案**：因为逐层参考在层间把残差以 bf16 落盘（`residual_list` 是 bf16），下一层读到的就是 bf16 值。融合 kernel 把残差留在 fp32 片上，若不主动 round-trip 到 bf16，fp32 累加的低位精度会让结果偏离参考，`torch.equal` 位精确对拍就会失败。删掉后，数值仍接近但不再位精确。

**练习 2**：kernel 的网格是 `T.Kernel(n, threads=n_thr)`（一维，按 token）。为什么不让每个 block 处理「一层」、再并行多层？换句话说，为什么层维度必须串行？

**参考答案**：因为层与层之间有**数据依赖**——第 `i+1` 层的输入残差是第 `i` 层 post 的输出。串行的层循环正是为了让 `res_local` 跨层滚动复用、始终留在片上。若把不同层分到不同 block 并行，层间残差就必须走 HBM 传递，正好毁掉了「残差不出片」这个核心收益。

**练习 3**：`ptx_wait_group(4)` 里的 `4` 对应什么？为什么最后一层用 `ptx_wait_group(0)`？

**参考答案**：`4` 对应每层预取的 4 个 `async_copy`（`layer_output / pre_mix / post_mix / comb_mix`）。迭代 `i` 发出下一层的 4 个预取后，`wait_group(4)` 等到 in-flight 数 ≤ 4，即让最早的 4 个（当前层）完成。最后一层不再发新预取，用 `wait_group(0)` 把剩余 in-flight 全部等齐。

---

### 4.2 ops 层封装与训练/推理两条路径

#### 4.2.1 概念说明

读完底层 kernel，现在看它在 `modeling/mhc/ops` 层的封装，以及它在整条 mhc 训练流水线里的位置。

**先纠正一个可能预期：ops 层目前并没有 autograd.Function 包装。** 打开 [modeling/mhc/ops/multilayer_recompute.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/multilayer_recompute.py)，整个文件只有三行：从底层 kernel 直接导入 `mhc_multilayer_recompute` 并放进 `__all__`。对比同目录其它 op：

| op | 是否有 `torch.autograd.Function` | 原因 |
| --- | --- | --- |
| `mhc_post`（[ops/post.py:6-25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/post.py#L6-L25)） | 有（`MHCPost`） | 是计算图里的可微 op，需 forward/backward |
| `sinkhorn_normalize`（见 u7-l3） | 有 | 同上，且手写反向 |
| `pre_big_fuse`（推理融合） | 无 | 推理专用，无反向实现 |
| **`mhc_multilayer_recompute`** | **无** | **是「重算原语」，供反向/重算上下文直接调用** |

**为什么 multilayer_recompute 不需要 autograd.Function？** 因为它**本身不参与自动微分图**。它的设计用途是：训练时在**反向阶段（或梯度检查点的重算阶段）**被外部训练框架直接调用，从已保存的检查点（`initial_residual` + 各层 mixes + 各层 `layer_output`）一次性重算出所有 `layer_input` 和 `residual`，供各层已注册的 backward（如 `MHCPost`、`pre_apply_mix` 的反向）复用。调用方通常会在 `torch.no_grad()` 或「不需要再记录计算图」的上下文里使用它。给这样一个叶子原语再套一层 `autograd.Function` 既无必要、也不符合它的定位。

> 类比：它像 PyTorch 里 `torch.utils.checkpoint` 内部调用的那些「重算前向」——重算时不需要再建图，只要数值。

#### 4.2.2 核心流程：训练 vs 推理两条路径

把视野拉到整条 mhc 流水线，对比训练与推理：

**推理路径（`grad disabled`）—— 单层内融合：**
- `mhc_pre` 检测到 `torch.is_grad_enabled()` 为 False，直接走 `pre_big_fuse` 四合一 kernel（[functional.py:69-82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69-L82)）。
- `pre_big_fuse` 把 norm_fn + split_mixes + sinkhorn + apply_mix 融进一个 kernel，靠 warp 分工让中间量留片上，**但没有反向实现**——所以只能在推理态启用（见 u7-l2）。
- 推理时不需要 `multilayer_recompute`：因为没有反向，也就没有「重算」需求。

**训练路径（`grad enabled`）—— 单层拆分 + 跨层重算：**
- 前向：`mhc_pre` 走四步拆分（norm_fn → split_mixes → sinkhorn → apply_mix），每步是独立 autograd.Function，自带反向（[functional.py:84-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L84-L105)）；post 段走 `MHCPost`。
- 为了省显存，训练框架可用**梯度检查点**：前向只存检查点（`initial_residual`、各层 mixes、各层 `layer_output`），不保留中间 `layer_input/residual`。
- 反向需要这些中间量时，调用 `mhc_multilayer_recompute` **一次性重算全部层的 `layer_input` 与 `residual`**，再分发给各层 backward 使用。

两者对比如下：

| 维度 | 推理（big_fuse） | 训练（拆分 + multilayer_recompute） |
| --- | --- | --- |
| 触发条件 | `not torch.is_grad_enabled()` | `torch.is_grad_enabled()` |
| 单层前向 | 一个 `pre_big_fuse` 融合 kernel | 四个独立 autograd.Function |
| 是否有反向 | **否**（无反向实现） | 是（各 op 自带 + 跨层重算） |
| 跨层优化 | 不需要（无反向） | 用 `multilayer_recompute` 把多层 pre+post 链一次性重算 |
| 主要收益 | 单层内省带宽（中间量留片） | 跨层级省带宽（层间残差不出片）+ 省显存（检查点） |
| 封装形态 | functional 层直接调 | ops 层暴露原语，由外部训练框架在反向调用 |

两条路径的共同精神是「**以不同方式用融合换带宽**」：推理敢于大融合是因为不需要反向；训练必须保留可反传的拆分路径，但可以用 `multilayer_recompute` 在重算阶段拿到「跨层融合」的带宽收益。

#### 4.2.3 源码精读

**(a) ops 层的纯 re-export。** [ops/multilayer_recompute.py:1-3](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/multilayer_recompute.py#L1-L3) 仅做导入与 `__all__` 声明。它把底层 kernel 的 wrapper 原样暴露到 ops 命名空间，没有附加任何 autograd 逻辑——这是它「重算原语」定位的直接体现。

**(b) ops 聚合出口。** [ops/__init__.py:3](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py#L3) 把它和 `expand / post / pre_* / sinkhorn / head_compute_mix` 一起导出。注意 `functional.py` 当前**并未**直接 import `multilayer_recompute`——这进一步说明它不是单层流水线的内部步骤，而是一个供外部（训练框架的反向/检查点逻辑）按需调用的独立工具。

**(c) 训练-推理分流点。** [functional.py:69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69) 的 `if not torch.is_grad_enabled():` 是两条路径的分水岭。`pre_big_fuse` 的 kernel（[pre_big_fuse_kernel.py:8-40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L8-L40)）只有一个前向 `prim_func`、没有配套 `_bwd`，对照 `post_kernel.py` 里成对的 `_mhc_post_fwd/_mhc_post_bwd`，就能直观看到「推理融合 kernel 无反向」这一事实。

#### 4.2.4 代码实践（源码阅读型，对应本讲 practice_task）

**实践目标**：厘清 `multilayer_recompute` 在 mhc 训练流水线中的位置，并与 `pre_big_fuse` 对比训练/推理两条路径的差异。

**操作步骤**：

1. 打开 [modeling/mhc/ops/multilayer_recompute.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/multilayer_recompute.py)，确认它只是 re-export、没有 `autograd.Function`。
2. 在 [ops/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py) 里数一下：哪些 op 有 `MHC*` 类（autograd.Function）、哪些没有。预期：`post` 有，`multilayer_recompute / pre_big_fuse` 没有。
3. 在 [functional.py:69-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69-L105) 标注：推理走 `mhc_pre_big_fuse`（无反向），训练走四步拆分（各有反向）。
4. 回答下面三个问题（见 4.2.5 答案）。

**需要观察的现象 / 预期结果**：

- `multilayer_recompute` 不出现在 `functional.py` 的 import 列表里——它是「旁路」工具，不是单层流水线的一环。
- `pre_big_fuse` 与 `multilayer_recompute` 都没有反向实现，但**原因不同**：前者是推理专用、省掉反向换带宽；后者是重算原语、由外部在反向阶段直接调用、本身不进 autograd 图。

> 说明：本实践为「源码阅读型」，无需 GPU 即可完成；若要在真实训练循环里验证重算路径的显存收益，需要自行搭建一个带梯度检查点的 mhc 训练脚本，这超出本讲范围，标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`multilayer_recompute` 和 `pre_big_fuse` 都没有 `torch.autograd.Function`，两者的原因分别是什么？

**参考答案**：`pre_big_fuse` 是**推理专用**的大融合 kernel，用「不实现反向」换取把单层 pre 的四步融进一个 kernel 的带宽收益，故只在 `not is_grad_enabled()` 时启用。`multilayer_recompute` 是**重算原语**，设计上在反向/检查点重算阶段被外部直接调用，调用方处于不需要再记录计算图的上下文，因此它本身不进 autograd 图、不需要 `autograd.Function`。

**练习 2**：假如你想把 `multilayer_recompute` 接进一个 PyTorch 训练循环的梯度检查点里，它应该在前向还是反向被调用？调用前后需要保存哪些量？

**参考答案**：应在**反向阶段**（重算时）调用。前向只需保存检查点：`initial_residual`、每层的 `pre_mix/post_mix/comb_mix`、每层的子层输出 `layer_output`。反向调用 `mhc_multilayer_recompute` 后，把写出的 `layer_input_list` 和 `residual_list` 分发给各层已注册的 backward 使用。注意调用宜在 `torch.no_grad()` 上下文，避免重算本身又被记录进图。

**练习 3**：benchmark 测试里有个 `theory = io_ref / io_fused`（[test_multilayer_recompute.py:79-88, 125](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_multilayer_recompute.py#L79-L125)）。从 `io_ref` 与 `io_fused` 的表达式看，融合主要省掉了哪部分流量？

**参考答案**：主要省掉了**层间残差的重复读取**。`io_ref` 里每个 pre 和每个 post 都各读一次残差（共 `num_layers + num_post` 次读），而 `io_fused` 只在开头读一次 `initial_residual`、之后残差全部留在片上；两者的残差**写**次数相同（都是 `num_post` 次）。因此 `theory` 近似为「总流量 / 去掉冗余残差读后的流量」，是纯带宽受限下的理论上限。

---

## 5. 综合实践

**任务**：把本讲的两条线索（跨层融合的带宽收益 + 训练/推理路径分工）串起来，做一次「纸带式」流量核算与定位。

1. **算一次理论收益**。取测试用例 `(num_layers=10, num_post=9, hidden=8192)`、`n=bs*seq=1*8192`、`mhc_mult=4`：
   - 用 [test_multilayer_recompute.py:79-88](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_multilayer_recompute.py#L79-L88) 的公式手算 `io_ref` 与 `io_fused`，求 `theory = io_ref/io_fused`。
   - 分解出「被省掉的残差读流量」= `(num_layers + num_post - 1) * n * mhc_mult * hidden * 2` 字节，验证它占 `io_ref` 的比例与 `theory - 1` 同量级。
2. **定位三个 kernel 的反向实现**。在 `tile_kernels/mhc/` 下找出：哪个 kernel 有成对的 `_fwd/_bwd` prim_func（如 `post_kernel.py`），哪些只有单个前向（如 `pre_big_fuse_kernel.py`、`multilayer_recompute_kernel.py`），把结论填进 4.2.1 的对比表。
3. **画出调用时序**。画两张时序图：
   - 推理：`mhc_pre → big_fuse`（单层一融合，无反向）。
   - 训练：前向 `mhc_pre → 四步拆分` + 子层 + `mhc_post`；反向 `multilayer_recompute`（跨层重算）→ 各层 backward。

**交付物**：一张流量核算表 + 一张「有/无反向」分类表 + 两张时序图。

> 说明：步骤 1 的数值手算可在无 GPU 环境完成；若要拿到实测 `efficiency = speedup/theory`，需在有 SM90/SM100 GPU 与正确依赖的环境运行 benchmark（`pytest tests/mhc/test_multilayer_recompute.py -k benchmark --run-benchmark`），该部分标注「待本地验证」。

## 6. 本讲小结

- `mhc_multilayer_recompute` 把 mhc 的 L 层 pre+post 链融合进**单个 TileLang kernel**，让中间残差 `res_local` 跨层驻留片上，省掉层间残差的 HBM 往返——一次纯带宽优化，数学与「逐层 pre_apply_mix + post」完全等价。
- 层间串行（`T.serial`）是有意为之：层间残差有数据依赖，串行才能让残差不出片；网格按 token 一维划分，hidden 按 `h_blk` 分块。
- 「指针表 + `T.make_tensor`」让一份按层数特化的编译产物，能在运行时寻址可变的张量列表；`_make_ptr_tables_batched` 负责把 `data_ptr()` 收集成 GPU 上的指针数组。
- 双缓冲 `T.async_copy` + `T.ptx_wait_group(4/0)` 构成软件流水线，让「搬下一层」与「算当前层」重叠。
- 每层 post 后把 `res_local` 强制 bf16 往返，保证与逐层参考**位精确**一致（测试用 `torch.equal`）。
- ops 层封装目前是**纯 re-export，没有 autograd.Function**——它是「重算原语」，定位与推理专用的 `pre_big_fuse`（同样无反向）不同：训练用拆分 + 跨层重算，推理用单层 big_fuse。

## 7. 下一步学习建议

- **回归 autograd 封装范式**：本讲看到 `multilayer_recompute` 不需要 autograd.Function，下一步可对照 [modeling/mhc/ops/post.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/post.py) 的 `MHCPost`（u8-l1 会系统讲 `torch.autograd.Function` 封装范式），理解「何时该套 Function、何时不该套」。
- **深入双缓冲与异步拷贝**：本讲的 `T.async_copy` / `T.ptx_wait_group` 是软件流水线的入口，建议回看 [post_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py) 里 `T.Pipelined(..., num_stages=2/3)` 的另一种流水线写法，对比两种风格。
- **端到端把重算接进训练**：若你想真正跑通训练路径，建议阅读一个带梯度检查点的 Transformer 实现，尝试把 `mhc_multilayer_recompute` 插入其反向重算阶段（本讲范围外，可作为进阶实战）。
- **测试与基准设施**：本讲引用了 `theory / efficiency / bandwidth_gbs` 等基准指标，下一阶段（u9 单元）会系统讲 benchmark 插件与回归检测机制。
