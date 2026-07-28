# 自定义输入 CustomIO 的降级

## 1. 本讲目标

本讲承接 u2-l5（score_mod 降级）与 u2-l6（online_func 降级），打开编译链上第三块降级拼图：**自定义输入 `CustomIO` 的降级**——也就是 `lower_custom_inputs`。

学完后你应该能够：

1. 说清一个 custom input 张量（如 `softmax_bias`）从用户的一行 `CustomIO({"softmax_bias": (1,)})` 到最终 kernel 里那段加载代码，中间经历了哪些步骤。
2. 理解 GPU 片上三级内存 **global → shared → fragment** 的区别，以及 `lower_custom_inputs` 为什么会根据张量形状走三条不同的加载分支。
3. 掌握 `shape_idx`（用户形状）到片上分块形状（`block_M`/`block_N`/`dim`）的下标推导，看懂 `load_op` 生成的 `T.copy(...)` / 标量赋值代码。
4. 理解 `swizzle layout`（`make_swizzled_layout`）在 shared memory 分支里消除 bank conflict 的作用。

## 2. 前置知识

### 2.1 为什么需要 CustomIO

回顾 u1-l4：一次注意力的核心计算是 `scores = q @ k` → 逐元素 `score_mod` → 行级 `online_func` → `o = p @ v`。其中 `score_mod` 的签名是 `score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx)`，第二个参数 `custom_fwd_inputs` 用来传入 q/k/v **之外**的、用户自定义的额外输入张量。

最典型的例子是带偏置的注意力：`score = score + softmax_bias`。这里的 `softmax_bias` 是一个可学习参数，编译时框架只关心它的**形状**（用来生成 kernel 的参数声明），运行时才传入真正的张量。`CustomIO` 就是用来在编译期声明这些额外输入形状的容器（见 u1-l4）。

### 2.2 GPU 三级内存速览

要把 global 上的数据送进计算单元，GPU 上数据一般要经过三级：

| 层级 | TileLang 表达 | 位置 | 访问速度 | 容量 |
|------|--------------|------|----------|------|
| global memory | kernel 输入 `T.Buffer(...)` | 显存（HBM） | 最慢 | 最大 |
| shared memory | `T.alloc_shared(...)` | 片上（SM 内） | 快（需 `T.copy` 搬运） | 小（几十 KB） |
| fragment / register | `T.alloc_fragment(...)` | 寄存器 | 最快 | 极小 |

数据搬运路径通常是 `global → shared → fragment`。但是否需要经过 shared 这一级，取决于张量的访问模式——这正是本讲的核心。

### 2.3 符号对象回顾

- `SymbolScalar`（u2-l2）：带 `varname`/`shape_idx`/`dtype` 等簿记字段的符号值，这里被复用来描述一个张量的元信息。
- `shape_idx`：用一个**字符串维度名**列表描述形状，如 `["1"]`、`["block_N", "dim"]`，支持动态形状。
- `IndentedCode`（[utils.py:4-32](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/utils.py#L4-L32)）：一个带缩进的字符串累加器，`add_line` 会按当前缩进拼一行并补换行；降级函数大量用它来拼代码。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [attention_engine/core/lower/lower.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py) | 降级编排层。本讲主角 `lower_custom_inputs`、通用 kernel 生成 `lower_kernel`、形状映射表、`RECURRENT_DIM` 都在这里。 |
| [attention_engine/core/codegen/common.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py) | codegen 辅助函数：`load_op`/`store_op`/`copy_op`/`arg_def`/`alloc_*_op`，把符号张量翻译成具体的 `T.copy`/`T.alloc_*` 代码字符串。 |
| [attention_engine/core/transform/core.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py) | `CustomIO` 与 `SymbolicTensor` 的定义。 |
| [attention_engine/core/template/tl_template/attn/attn_tl.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py) | Jinja2 模板，降级产物 `{{custom_fwd_inputs_load_shared}}` 等占位符在这里被注入到 kernel 的对应位置。 |
| [attn_script/sigmoidattn.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py) | 本讲的实践样本：一个形状为 `(1,)` 的 `softmax_bias` 的 CustomIO 用例。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**三级内存映射**、**shape_idx 下标推导**、**swizzle layout**。三者最终汇合于 `lower_custom_inputs` 里的三条加载分支。

### 4.1 三级内存映射：global → shared → fragment

#### 4.1.1 概念说明

`lower_custom_inputs` 的职责是：对用户在 `CustomIO` 里声明的每一个额外输入张量，决定它**如何从 global 搬到片上、搬到哪里（shared 还是 fragment）、什么时候搬（进循环前还是每个 kv 块都搬）**。

关键洞察：注意力是一个**对 kv 序列分块循环**的算法（外层遍历 `k` 个 `block_N` 大小的 kv 块）。一个 custom input 张量如果在每个 kv 块里取值都一样（如标量偏置），就只需在循环前搬一次；如果取值随 kv 块变化（如依赖于 kv 位置的偏置），就必须每轮循环重新搬。这个「是否随 kv 块变化」的判据，就是张量形状里有没有 `seq_len_kv` 这一维——在片上映射后变成 `block_N`，代码里用常量 `RECURRENT_DIM = "block_N"` 表示。

#### 4.1.2 核心流程

`lower_custom_inputs` 对每个 custom input 走如下决策树（三条分支）：

```
对每个张量 k：
  计算 shape_idx_block（片上分块形状，去掉 batch/heads 等退化维）
  ├── 分支①：block_N 不在 shape_idx_block 中
  │     → 不依赖 kv 块，注册一个 CopyMap（global→fragment）
  │     → 由 lower_kernel 生成「循环前一次性」加载（prologue）
  │
  ├── 分支②：block_N 在 shape_idx_block 中，且是多维，且第 0 维不是 "1"
  │     → 依赖 kv 块，需每轮重载；数据量大、有特征维
  │     → 分配 shared 缓冲 + swizzle layout
  │     → 生成 global→shared（每轮）+ shared→fragment（每轮）两段
  │
  └── 分支③：block_N 在 shape_idx_block 中，但单维 或 第 0 维是 "1"
        → 依赖 kv 块，但形状简单
        → 直接 global→fragment（每轮），不经过 shared
```

注意命名上的「陷阱」：占位符叫 `custom_fwd_inputs_load_shared`（[attn_tl.py:121](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L121)），但它实际承接的是分支②和分支③的代码——分支②里确实是 global→shared，分支③里则是 global→fragment 的直接加载。真正只属于 shared 中转的是 `custom_fwd_inputs_load_s2r`（s2r = shared to register/fragment，[attn_tl.py:140](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L140)），它只在分支②非空。

#### 4.1.3 源码精读

整个函数在 [lower.py:560-614](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L560-L614)。

首先，函数遍历 `custom_fwd_inputs.input_tensors` 里的每个张量，分别登记一个 **fragment 片上张量**和一个 **global 输入张量**（注意名字加 `g_` 前缀）：

```python
# lower.py:582-583  登记片上 fragment 与 global 输入
kernel_options.fragment_tensors[k] = (SymbolScalar(k, Var(k), shape_idx=shape_idx_block, dtype=custom_input_dtype))
kernel_options.global_tensors_input[f"g_{k}"] = (SymbolScalar(f"g_{k}", Var(f"g_{k}"), shape_idx=v.shape_idx, dtype=custom_input_dtype))
```

随后是三条分支（[lower.py:586-602](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L586-L602)）：

**分支①（不含 block_N）——注册 CopyMap，交给通用机制：**

```python
# lower.py:586-589
if not (RECURRENT_DIM in shape_idx_block):
    kernel_options.copy_maps.append(
        CopyMap(kernel_options.global_tensors_input[f"g_{k}"], kernel_options.fragment_tensors[k], shape_idx_copy_sp, shape_idx_dim_map)
    )
```

这里**不直接生成代码**，而是登记一个 `CopyMap`。真正生成加载代码的是通用函数 `lower_kernel`（[lower.py:275-315](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L275-L315)），它遍历所有 `copy_maps`，把「源在 global_tensors_input」的那些翻译成 prologue 加载（[lower.py:308-315](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L308-L315)），填进模板占位符 `custom_fwd_inputs_load_prolog`（在循环**之前**，[attn_tl.py:106](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L106)）。

**分支②（含 block_N、多维、首维非 "1"）——shared 中转 + swizzle：**

```python
# lower.py:590-597
elif len(shape_idx_block) > 1 and shape_idx_block[0] != "1":
    custom_input_dtype = "dtype"
    kernel_options.shared_tensors[f"{k}_shared"] = (...)            # 分配 shared 缓冲
    custom_fwd_inputs_load_shared += str(load_op(g_{k} → {k}_shared) + "\n")  # global→shared
    custom_fwd_inputs_load_s2r    += copy_op({k}_shared → {k})      # shared→fragment
    lower_output.swizzle_shared += f"{k}_shared: tl.layout.make_swizzled_layout({k}_shared), \n"
```

这条分支会把 dtype 从 `accum_dtype` 改成 `dtype`（因为 shared 里通常存低精度原始数据，fragment 累加时再升精度）。它生成**两段**代码，分别注入到循环内的 `custom_fwd_inputs_load_shared` 和 `custom_fwd_inputs_load_s2r`。

**分支③（含 block_N、但单维或首维 "1"）——直接 global→fragment：**

```python
# lower.py:598-602
else:
    custom_fwd_inputs_load_shared += str(
        load_op(g_{k} → {k}, ...) + "\n"     # 直接 global→fragment，不经 shared
    )
```

> 设计要点：分支①把「形状简单、与 kv 块无关」的张量交给通用的 `lower_kernel`/`CopyMap` 机制（复用 score_mod、online_func 也会用到的同一套参数声明与 prologue 生成）；分支②③因为要在 **kv 循环内**重载，通用 prologue 机制（只在循环前执行）无法表达，所以直接把代码字符串累加到 `custom_fwd_inputs_load_shared`/`custom_fwd_inputs_load_s2r`，由模板放进循环体。

函数最终把三段代码字符串打包进 `customInputOutput` 返回（[lower.py:260-264](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L260-L264)，[lower.py:610-614](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L610-L614)）。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 `sigmoidattn.py` 里 `softmax_bias`（形状 `(1,)`）走了哪条分支、生成了什么代码。

`sigmoidattn.py` 的声明（[sigmoidattn.py:51-53](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L51-L53)）：

```python
custom_fwd_inputs = CustomIO({
    "softmax_bias": (1,),
})
```

操作步骤（源码阅读型实践）：

1. 打开 `attention_engine/core/lower/lower.py`，定位 `lower_custom_inputs`（第 560 行）。
2. 对 `softmax_bias`，`v.shape_idx = ["1"]`。逐步算出：
   - `shape_idx_block`：`shape_idx_onchip_map["1"]` 是 `""`，过滤掉空串后为 `[]`，再被兜底成 `["1"]`（[lower.py:574-576](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L574-L576)）。
3. 判定分支：`RECURRENT_DIM("block_N") in ["1"]` 为 `False`，所以 `not (...)` 为 `True` → **走分支①**（[lower.py:586](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L586)）。

**需要观察的现象 / 预期结果**：

- 分支①只登记了 `CopyMap`，没有触碰 `custom_fwd_inputs_load_shared` 或 `custom_fwd_inputs_load_s2r`，也没有写 `swizzle_shared`。
- 由 `lower_kernel` 为该 `CopyMap` 生成的 prologue 加载代码，经过 `load_op` 对 `shape=[1]` 的特判（[common.py:63-64](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L63-L64)），最终形如：

  ```python
  softmax_bias[0] = g_softmax_bias[0]
  ```

- 这段代码出现在 kernel 的 **prologue**（循环之前，`{{custom_fwd_inputs_load_prolog}}`，[attn_tl.py:106](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L106)），而不是 kv 循环体内。

**为何不走 shared 分支**：因为 `softmax_bias` 形状是 `(1,)`，对所有 batch/head/query/key 位置都是同一个广播标量，**不随 kv 块变化**。它只需在循环开始前加载一次到 fragment，没必要每轮重载，更不必占用宝贵的 shared memory。只有当张量形状包含 kv 序列维（`seq_len_kv`，片上映射为 `block_N`）时，才需要每轮重载，进而才可能进入分支②/③。

> 说明：以上生成代码的精确文本（如 `softmax_bias[0] = g_softmax_bias[0]`）来自对源码逻辑的静态推导；若要查看实际渲染产物，可参考 u5-l6 介绍的「导出/读取 cache 目录里生成的 TileLang 代码」的方法。标注为「待本地验证」的部分指运行后核对渲染文本。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `softmax_bias` 的形状从 `(1,)` 改成依赖 kv 位置的 `(seq_len_kv,)`（假设用户允许这么写），它会走哪条分支？为什么？

**参考答案**：会走**分支③**。因为 `shape_idx_block` 会变成 `["block_N"]`（含 `RECURRENT_DIM`），但 `len == 1` 不满足分支②的「多维」条件，落入 `else`。它会在每轮 kv 循环内直接 `global → fragment` 重载，但不经过 shared。

**练习 2**：分支①为什么把生成代码的活「外包」给 `lower_kernel`，而分支②③却自己直接拼字符串？

**参考答案**：分支①的加载发生在 **kv 循环之前**（prologue），与 q/k/v、final_rowscales 等其它输入的声明和加载共用同一套「函数参数声明 + prologue 拷贝」机制，复用 `CopyMap`/`lower_kernel` 最省事；分支②③的加载必须发生在 **kv 循环体内**，而通用 prologue 机制表达不了「循环内」这个位置，只能把代码字符串直接注入模板的循环体占位符。

---

### 4.2 shape_idx 下标推导：从用户形状到片上分块

#### 4.2.1 概念说明

`lower_custom_inputs` 要解决的核心数学问题：用户用**全局逻辑形状**（如 `(batch, heads, seq_len)`）描述张量，但 kernel 里操作的是**当前分块**（如 `[block_M, block_N]`）。需要一张映射表，把逻辑维度名翻译成：

1. **global 侧下标表达式**（在 `g_{k}[...]` 里取哪一段）；
2. **片上分块形状**（alloc 多大的 fragment/shared）；
3. **维度对应关系**（哪些 global 维对应哪些片上维，供 `load_op` 推导切片步长）。

#### 4.2.2 核心流程

lower.py 顶部定义了四张映射表（[lower.py:31-56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L31-L56)）：

| 表 | 作用 | 例 |
|----|------|----|
| `shape_idx_map_sp` | 逻辑维 → **global 下标** sympy 表达式 | `seq_len_kv → k*block_N` |
| `shape_idx_onchip_map` | 逻辑维 → **片上分块大小**字符串 | `seq_len_kv → block_N` |
| `shape_idx_onchip_step_map_sp` | 逻辑维 → **切片步长** sympy 表达式 | `seq_len_kv → block_N` |
| `shape_idx_onchip_dim_map` | 需要切片（而非标量）的维度名集合 | `["seq_len", "seq_len_kv"]` |

在 `lower_custom_inputs` 里，对一个张量的 `shape_idx`（逻辑维名列表）逐维查表，得到四组并行信息（[lower.py:568-579](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L568-L579)）：

```python
shape_idx_copy_sp    = [shape_idx_map_sp[s] ...]         # global 下标表达式列表
shape_idx_block      = [shape_idx_onchip_map[s] ...]     # 片上分块大小（去掉 "" 退化维）
shape_idx_block_step_sp = [shape_idx_onchip_step_map_sp[s] ...]  # 切片步长
shape_idx_dim_map    = [idx for idx,s in enumerate(shape_idx) if s in shape_idx_onchip_dim_map]  # 需切片的维度下标
```

`load_op`（[common.py:32-66](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L32-L66)）据此生成切片表达式。其索引算术为：对每一维 `i`，

\[
\text{start}_i = \text{src\_idx\_list}[i], \qquad \text{end}_i = \text{start}_i + \text{step}_i
\]

- 若 `step_i == 0`（标量维）：写成 `start_i`；
- 否则（切片维）：写成 `start_i : end_i`。

当 `src_step_list` 未显式提供时，步长由 `dim_map_list`（片上维 ↔ global 维的对应）反推：`src_step_list[src_dim] = dst.shape[dst_dim]`（[common.py:48-51](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L48-L51)）。

#### 4.2.3 源码精读

以 `seq_len_kv` 维为例，它会被翻译成「第 k 个 kv 块」：

- global 下标：`k*block_N`（起点）；
- 步长：`block_N`；
- 所以切片为 `k*block_N : (k+1)*block_N`。

这正是模板里 `K`、`V` 的加载写法 `K[bz, k*block_N : (k+1)*block_N, by, :]` 的来源——`lower_custom_inputs` 对含 `seq_len_kv` 维的 custom input 会生成同样形态的切片。

对于 `batch`/`heads` 维，`shape_idx_map_sp` 给的是 `bz`/`by`（当前 batch/head 的标量下标），步长为 0，所以它们写成标量 `bz`、`by`，不切片。

特殊地，`"1"` 维被 `shape_idx_map_sp` 映射为 `0`（标量下标 0），片上映射为空串 `""`（被过滤掉，不占片上维度）。当片上形状最终是 `[1]` 时，`load_op` 走特判，生成标量赋值而非 `T.copy`（[common.py:63-64](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L63-L64)）——注释 `# tl copy bug when "1"`（[lower.py:585](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L585)）说明这是为了规避 TileLang 对形状 `[1]` 张量做 `T.copy` 的已知问题。

与 `load_op` 对偶的 `store_op`（[common.py:68-85](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L68-L85)）用于把片上结果写回 global（final_rowscales 的落盘），逻辑对称。而无脑整块拷贝用 `copy_op`（[common.py:87-88](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L87-L88)），即 `T.copy(src, dst)`，分支②的 shared→fragment 就用它。

#### 4.2.4 代码实践

**实践目标**：手工推导一个形状为 `(heads, seq_len_kv, dim)` 的 custom input（假想一个「每个 head、每个 kv 位置、每个特征通道」的偏置 `pos_bias`）在分支②会生成的下标。

操作步骤：

1. 设 `shape_idx = ["heads", "seq_len_kv", "dim"]`。
2. 查表：
   - `shape_idx_copy_sp = [by, k*block_N, 0]`（`dim` 不在 map 里，兜底 `0`，见 [lower.py:568-569](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L568-L569)）；
   - `shape_idx_block`：`heads→""`、`seq_len_kv→"block_N"`、`dim→"dim"`，过滤空串得 `["block_N", "dim"]`；
   - `shape_idx_dim_map`：只有 `seq_len_kv`（下标 1）在 `shape_idx_onchip_dim_map` 中，所以 `[1]`。
3. 判定分支：含 `block_N`、`len==2>1`、首维 `"block_N" != "1"` → **分支②**。

**预期结果（待本地验证）**：

- `custom_fwd_inputs_load_shared` 段会生成（示意，非逐字）：

  ```python
  T.copy(g_pos_bias[by, k*block_N:k*block_N+block_N, :], pos_bias_shared)
  ```

  其中 `by` 是当前 head 标量、`k*block_N:(k+1)*block_N` 是当前 kv 块、`dim` 维整段取（步长由片上 `dim` 反推）。

- `custom_fwd_inputs_load_s2r` 段生成 `T.copy(pos_bias_shared, pos_bias)`。
- `swizzle_shared` 段追加 `pos_bias_shared: tl.layout.make_swizzled_layout(pos_bias_shared),`。

需要观察：`seq_len_kv` 维被切成块，而 `heads`/`dim` 分别退化为标量 `by` 和整段 `:`。

#### 4.2.5 小练习与答案

**练习 1**：`shape_idx_dim_map` 为空（如 `softmax_bias` 的 `(1,)`）意味着什么？

**参考答案**：意味着没有任何一维需要切片，所有维要么是标量下标（步长 0），要么是 `"1"`。`load_op` 因此只会生成标量赋值，不会出现 `a:b` 切片。

**练习 2**：为什么 `dim`（特征维）在 `shape_idx_map_sp` 里查不到，却仍能正确生成 `:` 整段取？

**参考答案**：查不到时 `shape_idx_copy_sp` 兜底为 `0`（[lower.py:568-569](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L568-L569)），但 `load_op` 的步长由 `dim_map_list` 反推片上对应维的大小（`dim` 维片上大小就是 `dim`），最终 `start=0, end=0+dim`，写成 `0:dim`——但因为 `dim` 不在 `shape_idx_onchip_dim_map` 里，实际 `shape_idx_dim_map` 不含它；具体是否进 `dim_map_list` 取决于分支②把它视作切片维。此处细节标注「待确认」，建议结合 cache 目录实际渲染产物核对。

---

### 4.3 swizzle layout：bank conflict 消除

#### 4.3.1 概念说明

分支②会为 custom input 额外分配一块 **shared memory** 缓冲（`{k}_shared`），并给它标注一个 **swizzle layout**。这涉及 GPU shared memory 的物理结构。

GPU 的 shared memory 被划分成 32 个 **bank**，每个 bank 宽 4 字节，按地址循环分布。一个字的 bank 号为：

\[
\text{bank} = \left\lfloor \frac{\text{byte\_offset}}{4} \right\rfloor \bmod 32
\]

当一个 warp（32 线程）里的多个线程同时访问**同一 bank** 的不同地址时，就会发生 **bank conflict**，访问被串行化，带宽骤降。注意力里的 `block_M × block_N` 大 tile 如果按朴素行优先/列优先存放，GEMM 取数时极易产生冲突。

**Swizzle**（搅动）的思想：用一个可逆的位运算「打散」地址，让本会撞 bank 的访问被均匀分散到 32 个 bank 上，同时保持可逆性（存得进、取得出）。`tl.layout.make_swizzled_layout` 就是 TileLang 提供的现成 swizzle 布局。

#### 4.3.2 核心流程

分支②里，每登记一个 `{k}_shared`，就同步往 `lower_output.swizzle_shared` 追加一行（[lower.py:597](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L597)）：

```python
lower_output.swizzle_shared += f"{k}_shared: tl.layout.make_swizzled_layout({k}_shared), \n"
```

这段字符串最终被注入模板的 `T.annotate_layout({...})` 块，与 `Q_shared`、`scores_shared` 等并列（[attn_tl.py:100-104](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L100-L104)）：

```python
T.annotate_layout({
    Q_shared: tl.layout.make_swizzled_layout(Q_shared),
    scores_shared: tl.layout.make_swizzled_layout(scores_shared),
    {{swizzle_shared | indent(20)}}      # ← custom input 的 shared 布局注入这里
})
```

`T.annotate_layout` 的作用是告诉 TileLang 编译器：「这块 shared buffer 请用我指定的布局来分配地址」，从而让后续 `T.copy(global→shared)` 和 `T.copy(shared→fragment)` 的访存自动按 swizzle 后的地址进行，避免 bank conflict。

#### 4.3.3 源码精读

为什么只有分支②需要 swizzle？因为：

- **分支①**：custom input 直接进 fragment（寄存器），不占 shared，没有 bank conflict 问题。
- **分支③**：虽然每轮重载，但形状简单（单维或首维 `"1"`），直接 `global→fragment`，也不经过 shared。
- **分支②**：数据有 `block_N` 维和特征维，是大 tile，要进 shared 给后续 GEMM 用——这正是 bank conflict 高发区，必须 swizzle。

注意 `swizzle_shared` 是 `lowerOutput` 的字段（[lower.py:184](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L184)），它跨越 `lower_custom_inputs`/`lower_score_mod`/`lower_online_func` 共享——也就是说，**任何**降级函数都可以往里追加需要 swizzle 的 shared 张量，最后统一注入模板。这是一种「全局收集、一次注入」的协作模式。

#### 4.3.4 代码实践

**实践目标**：观察 `swizzle_shared` 的「全局收集」特性。

操作步骤：

1. 全仓搜索 `swizzle_shared`（本讲已确认在 `lower.py` 中，分支②是唯一写入点）。
2. 阅读 [attn_tl.py:100-104](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L100-L104)，确认它被注入到 fwd kernel 的 `T.annotate_layout`。
3. 对照同一文件的 bwd kernel（约 [attn_tl.py:326-328](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L326-L328)），看 bwd 的 `T.annotate_layout` 里 `K_shared`/`dv_shared`/`dk_shared` 也是同样的 swizzle 模式。

**预期结果**：fwd 与 bwd 模板都遵循「hardcode 的主权重张量 swizzle + `{{swizzle_shared}}` 占位符承接 custom input 的 swizzle」这一统一写法。custom input 的 shared 缓冲因此能享受与 `Q_shared` 同等的 bank conflict 优化。

#### 4.3.5 小练习与答案

**练习 1**：如果不做 swizzle（删掉分支②里那一行 `swizzle_shared += ...`），程序功能上还正确吗？性能会怎样？

**参考答案**：功能上仍然正确——swizzle 只改变 shared memory 的**地址映射**，不改逻辑数据，存取互逆。但性能会下降：大 tile 的 GEMM 访问会撞 bank，shared memory 带宽利用率降低，kernel 变慢。

**练习 2**：为什么 `swizzle_shared` 放在公共的 `lowerOutput` 里，而不是 `customInputOutput` 里？

**参考答案**：因为需要 swizzle 的 shared 张量可能来自多个降级阶段（custom input 是其中之一），用公共字段统一收集，再由模板一次性注入 `T.annotate_layout`，避免每个降级函数各自维护一份布局声明、且能在同一个 `annotate_layout` 调用里集中表达。

---

## 5. 综合实践

把本讲三个模块串起来：**为一个假想的「带可学习 kv 位置偏置」的注意力声明 CustomIO，并预测它的降级产物。**

设想 `score_mod` 需要一个形状为 `(seq_len_kv,)` 的偏置 `kv_bias`（每个 key 位置一个标量偏置）。请按以下步骤完成：

1. **声明**：在某个示例脚本里写 `custom_fwd_inputs = CustomIO({"kv_bias": ("seq_len_kv",)})`，并在 `score_mod` 里 `score = score + kv_bias`（参考 [sigmoidattn.py:14-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L14-L18) 的写法）。
2. **预测分支**：推导 `shape_idx_block`，判定它会走分支①/②/③中的哪一条。（提示：`seq_len_kv` 片上映射为 `block_N`，单维 → 分支③。）
3. **预测代码**：写出 `custom_fwd_inputs_load_shared` 会注入到 kv 循环体内（[attn_tl.py:121](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L121)）的加载语句形态（应含 `k*block_N:(k+1)*block_N` 切片）。
4. **预测是否 swizzle**：判断会不会往 `swizzle_shared` 写东西。（答案：不会，因为分支③不分配 shared。）
5. **验证**：按 u5-l6 的方法导出/读取生成的 TileLang 代码（或对照 cache 目录），核对你的预测是否一致。标注「待本地验证」的部分即指这一步。

这个任务把「三级内存映射（为何走 fragment 不走 shared）」「shape_idx 下标推导（kv 切片）」「swizzle layout（何时才需要）」三者一次打通。

## 6. 本讲小结

- `lower_custom_inputs`（[lower.py:560-614](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L560-L614)）为每个 custom input 张量登记 fragment/global 元信息，并按**是否含 kv 序列维（`block_N`/`RECURRENT_DIM`）**走三条加载分支。
- **分支①**（不含 `block_N`，如 `softmax_bias (1,)`）：注册 `CopyMap`，由通用 `lower_kernel` 生成循环前的 prologue 加载，只搬一次。
- **分支②**（含 `block_N`、多维、首维非 `"1"`）：经 `global → shared → fragment` 两段，分配 shared 缓冲并标注 swizzle layout，每轮 kv 循环重载。
- **分支③**（含 `block_N`、但单维或首维 `"1"`）：每轮直接 `global → fragment`，不经 shared。
- **shape_idx 下标推导**靠四张映射表（`shape_idx_map_sp`/`onchip_map`/`onchip_step_map_sp`/`onchip_dim_map`）把逻辑维翻译成 global 切片下标与片上分块形状，最终由 `load_op`/`store_op`/`copy_op`（[common.py:32-88](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/codegen/common.py#L32-L88)）落成代码。
- **swizzle layout**（`make_swizzled_layout`）只对分支②的 shared 缓冲启用，用于消除 shared memory 的 bank conflict；通过公共字段 `lower_output.swizzle_shared` 全局收集、由模板统一注入 `T.annotate_layout`。

## 7. 下一步学习建议

本讲完成了「降级三件套」的第三件——custom inputs。下一讲 **u2-l8（Mask 机制）** 会讲最后一块降级拼图：`mask_mod` 如何经 `torch.fx` 符号追踪降级成 kernel 内的掩码代码，以及 `infer_mask` 如何在 dense（`TlAttnTemplate`）与 blocksparse（`TlBlockAttnTemplate`）模板间选择。

完成 u2 全部降级讲义后，建议进入 u3-l1（Jinja2 模板渲染）回头看本讲产物 `custom_fwd_inputs_load_shared`/`custom_fwd_inputs_load_s2r`/`swizzle_shared` 是如何被 `attn_tl.py` 渲染进最终 kernel 的，形成「降级 → 模板」的闭环认识。若想看到真实渲染文本，可结合 u5-l6（测试与调试技巧）介绍的导出/读取 cache 目录方法进行核对。
