# Constexpr 特化与 @cute.jit 注入

## 1. 本讲目标

本讲承接 [u11-l1 JIT 编译与缓存机制](u11-l1-jit-and-cache.md) 的「JIT 把 Python 源码编译成 PTX/CUBIN」结论，往下追问一个更具操作性的问题：**到底哪些参数会改变 kernel 生成的机器码，哪些不会？**

具体来说，读完本讲你应该能够：

1. 分清 FA4 里两类「编译期常量」机制：显式 `cutlass.Constexpr` 参数注解，与通过 `self.*` 捕获进 kernel 的 Python 常量。
2. 看懂 `score_mod` / `mask_mod` 作为 `@cute.jit` 回调，是如何在编译期被「内联」进 kernel 主体的，以及为什么它们不能用普通函数指针实现。
3. 说出 `causal`、`head_dim`、`tile_m/n`、`pack_gqa`、`score_mod`、`mask_mod` 等参数变化时，分别走的是哪条「触发重编译」的路径，并能用 `compile_key` 验证。
4. 自己设计一个实验，用计时与缓存命中观察「切换 `causal` / `pack_gqa` 是否重新编译」。

---

## 2. 前置知识

本讲默认你已掌握以下内容（前置讲义已建立）：

- **FA4 的 kernel 是 Python + CuTeDSL 写的，运行时 JIT 编译**（见 u1-l3、u11-l1）。这与 FA2/FA3 安装期 `nvcc` 编译截然不同：FA4 第一次调用 `flash_attn_func` 时才编译，之后命中缓存即秒返回。
- **`compile_key` 决定是否重编译**（见 u11-l1）：它是一个由若干字段组成的元组，相等则复用已编译产物，不等则触发重新编译。
- **在线 softmax 与 score_mod / mask_mod 的语义**（见 u4-l1、u4-l2、u3-l1）：`score_mod` 在 softmax 之前修改分数 `S`，`mask_mod` 把非法位置置为可见性（最终把分数改成 `-inf`）。
- **前向主循环的数据流**（见 u6-l1）：加载 Q tile → 流水遍历 K/V block → 在线 softmax 累加 → 存 O 与 LSE。

还需要补充一个本讲要用到的、CuTeDSL 自身的基础概念：

- **特化（specialization）**：同一份 kernel 源码，根据「编译期已知的常量」不同，可以编译出多份不同的机器码。例如 `causal=True` 和 `causal=False` 会编译成两个不同的 kernel 二进制，前者干脆没有「掩码上三角」的指令。特化是「用编译时间换运行时性能」的典型手段：把本可在运行时判断的分支，在编译期就删掉，让生成的指令更精简、更少分支预测失败。

本讲要回答的核心矛盾是：**kernel 需要为「无数种配置」各编译一份，那这套机制如何既高效（避免无谓重编译）又正确（该特化时一定要特化）地组织起来？**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flash_attn/cute/flash_fwd.py` | 前向 kernel 基类。`__init__` 里用 `self.*` 捕获编译期常量；`kernel` 用 `cutlass.Constexpr` 标注少数显式编译期参数；主循环里用 `const_expr(...)` 删除分支。 |
| `flash_attn/cute/softmax.py` | `call_score_mod` 与 `apply_score_mod_inner`：`score_mod: cutlass.Constexpr` 的回调被内联调用。 |
| `flash_attn/cute/mask.py` | `call_mask_mod` 与 `AttentionMask.apply_mask`：`mask_mod: cutlass.Constexpr` 的回调被内联调用，并用 `const_expr` 在编译期裁剪掉无用掩码分支。 |
| `flash_attn/cute/interface.py` | `_flash_attn_fwd`：构造 `compile_key`，把 `causal`、`pack_gqa`、`tile_m/n`、`arch` 与 `score_mod/mask_mod` 的哈希都纳入键，决定是否调用 `cute.compile`。 |
| `flash_attn/cute/utils.py` | `hash_callable` / `create_softcap_scoremod`：对用户回调求源码哈希；`softcap` 参数被翻译成一个内置的 `score_mod`。 |
| `flash_attn/cute/cute_dsl_utils.py` | `StaticTypes` 元组：CuTeDSL 判定「何为编译期常量」的正式定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 Constexpr 编译期常量：特化的基石

#### 4.1.1 概念说明

先厘清两个容易混淆的符号，它们都来自 `cutlass`，但作用完全不同：

- **`cutlass.Constexpr`（大写、类型）**：写在 kernel 参数注解上的**编译期常量标记**。形如 `score_mod: cutlass.Constexpr` 或 `TileScheduler: cutlass.Constexpr[Callable]`。被它标注的参数，其**值**会成为 kernel 特化的一部分：值不同，就编译出不同的 kernel。
- **`cutlass.const_expr(x)`（小写、函数）**：写在 kernel 函数体里、用来把一个条件**强制在编译期求值**的辅助函数。最典型的用法是 `if const_expr(cond):`，它让 CuTeDSL 在编译期就决定保留还是删掉这个 `if` 分支，运行时不会有任何判断指令。它要求 `cond` 在编译期已知（来源于 `Constexpr` 参数、或 `self.*` 上的 Python 常量）。

一句话区分：**`Constexpr` 标记「这个参数是编译期的」；`const_expr(...)` 在函数体里「以编译期的方式使用某个值」**。

此外，FA4 kernel 是方法（绑定在 `self` 上），`self` 上的属性如果都是普通 Python `int` / `bool`（例如 `self.is_causal`、`self.tile_m`），它们在 JIT 编译时同样是**编译期常量**。CuTeDSL 用一个元组 `StaticTypes` 明确界定哪些 Python 值算静态：

[cute_dsl_utils.py:18](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py#L18)

```python
StaticTypes = (cutlass.Constexpr, NumericMeta, int, bool, str, float, type(None))
```

也就是说，`int`、`bool`、`str`、`float`、`None` 以及被 `Constexpr` 包裹的对象，都会被当作编译期常量烘焙进 kernel。这正是「为什么 `self.is_causal = True` 会让 kernel 特化」的根因——`True` 是 `bool`，属于 `StaticTypes`。

> 术语提示：CuTeDSL 文档里有时把这种「按编译期常量生成不同代码」称为 **specialization**（特化），把同一段源码编译出的不同二进制称为不同 **specialization**。本文统一用「特化」。

#### 4.1.2 核心流程

一个 FA4 前向 kernel 被特化的链路如下：

```text
用户调用 flash_attn_func(causal=True, pack_gqa=True, ...)
        │
        ▼
interface.py: _flash_attn_fwd(...)
   ├─ 把 causal / pack_gqa / tile_m / tile_n / arch / ... 组成 compile_key
   ├─ 把 score_mod / mask_mod 求哈希后放进 compile_key
   └─ 若 compile_key 不在缓存 → cute.compile(kernel, ...)
        │
        ▼
kernel.__init__ / self.* 被烘焙为编译期常量（is_causal、tile_m、pack_gqa …）
        │
        ▼
@cute.kernel kernel(...) 编译时：
   ├─ 显式 Constexpr 参数（SharedStorage / TileScheduler）取值确定
   ├─ self.* 上的 Python 常量在 const_expr(...) 处被静态求值
   ├─ 无用 if 分支被删除（特化掉）
   └─ 生成一份针对当前配置的 PTX → CUBIN
```

关键在于：**「特化」是在编译期完成的**。一旦编译完，运行时拿到的是「为这一组常量量身定做」的二进制，没有任何 `if causal:` 的运行时判断。

#### 4.1.3 源码精读

**(a) `self.*` 作为编译期常量**

前向 kernel 基类的 `__init__` 把所有配置都存成普通 Python 属性。注意 `score_mod` / `mask_mod` 被标注为 `cutlass.Constexpr`：

[flash_fwd.py:56-58](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L56-L58)

```python
score_mod: Optional[cutlass.Constexpr] = None,
mask_mod: Optional[cutlass.Constexpr] = None,
has_aux_tensors: bool = False,
```

而 `is_causal`、`is_local`、`pack_gqa`、`tile_m`、`tile_n`、`num_stages`、`num_threads` 等都是普通 `bool` / `int`（见 [flash_fwd.py:48-60](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L48-L60)）。它们随后都被 `self.xxx = xxx` 捕获。由于 `bool`/`int` ∈ `StaticTypes`，这些属性在 JIT 编译时全部是编译期常量。

一个值得注意的细节：连「向量化宽度」这种内部派生量也被显式声明成 `cutlass.Constexpr`，好让它在编译期参与代码生成：

[flash_fwd.py:103-116](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L103-L116)

```python
self.score_vec_size: cutlass.Constexpr = getattr(
    score_mod, "__vec_size__", 1 if cutlass.const_expr(has_aux_tensors) else 2
)
...
self.mask_vec_size: cutlass.Constexpr = getattr(mask_mod, "__vec_size__", 1)
```

这里的 `getattr(score_mod, "__vec_size__", ...)`：如果用户提供了一个带 `__vec_size__` 属性的 score_mod（表示它一次能处理多少列），就用那个值；否则默认。无论哪种，结果都被声明成 `Constexpr`，意味着「score_mod 的向量化宽度」也是特化的一部分——换一个不同宽度的 score_mod，会触发重编译。

**(b) `const_expr(...)` 删除运行时分支**

进入 kernel 主体后，到处都是 `if const_expr(...)`。它们的作用不是「运行时判断」，而是「编译期裁剪」。看主循环里对 `pack_gqa` 的处理：

[flash_fwd.py:798](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L798)

```python
qhead_per_kvhead_packgqa=self.qhead_per_kvhead if const_expr(self.pack_gqa) else 1,
```

`self.pack_gqa` 是编译期已知的 `bool`。`const_expr(self.pack_gqa)` 在编译期求值：

- 若 `pack_gqa=True`，这行变成 `qhead_per_kvhead_packgqa=self.qhead_per_kvhead`；
- 若 `pack_gqa=False`，变成 `qhead_per_kvhead_packgqa=1`。

两份编译产物里，这一行的运行时代码完全不同。`BlockInfo` 拿到的就是一个确定的整数，不会再有 `if pack_gqa:` 的判断开销。

同样的模式贯穿整个 kernel：[flash_fwd.py:822](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L822)（`num_head_kv = num_head if const_expr(self.pack_gqa) else num_head // self.qhead_per_kvhead`）、[flash_fwd.py:830](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L830)、[flash_fwd.py:842](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L842) 等。

**(c) 显式 `Constexpr` 参数注解**

绝大多数编译期常量是通过 `self.*` 隐式传入的，但有两个 kernel 参数被**显式**标注为 `Constexpr`。它们之所以要显式，是因为它们的「值」是一个复杂对象（类型 / 可调用对象），不是简单标量：

[flash_fwd.py:777-779](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L777-L779)

```python
SharedStorage: cutlass.Constexpr,
tile_sched_params,
TileScheduler: cutlass.Constexpr[Callable],
```

- `SharedStorage: cutlass.Constexpr`：共享内存的结构体类型（一个 `@cute.struct` 类）。不同的 tile/stages 配置会生成不同的共享内存布局类，类型本身是编译期已知的，标注为 `Constexpr` 让编译器据此分配 smem。
- `TileScheduler: cutlass.Constexpr[Callable]`：tile 调度器类（如 `SingleTileScheduler`）。`[Callable]` 表示「这是个编译期常量，且其类型是可调用对象」。kernel 里随后 `TileScheduler.create(...)` 直接调用它，因为类在编译期已确定，调用可以静态解析。

对照之下，紧挨着的 `softmax_scale_log2: Float32`、`window_size_left: Optional[Int32]`、`mQ: cute.Tensor` 等**没有** `Constexpr` 注解——它们是运行期参数，值的变化**不会**触发重编译（这正是 u11-l1 讲过的「`softmax_scale` 数值不进 compile_key」的代码层面原因）。

**(d) `@cute.kernel` vs `@cute.jit`**

注意两类装饰器：[flash_fwd.py:750](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L750) 的 `@cute.kernel def kernel(...)` 是真正的 GPU 入口（被 `cute.compile` 编译并启动 grid）；而大量 `@cute.jit def xxx(...)`（如 `apply_score_mod`、`compute_one_n_block`、`epilogue`）是「JIT 编译的辅助函数」，它们会被内联进 kernel 主体，同样享受编译期特化。两者面对 `Constexpr` / `const_expr` 的规则一致。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `self.*` 上的 Python 常量确实是编译期常量——通过观察「它出现在 `compile_key` 里，故改变它必触发重编译」。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 `flash_attn/cute/interface.py`，定位 `compile_key` 的构造处 [interface.py:720-767](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L720-L767)。逐项数出哪些字段是「`self.*` 上那类 Python 常量」：`causal`、`pack_gqa`、`tile_m`、`tile_n`、`num_threads`、`arch`、`head_dim`、`qhead_per_kvhead` …… 它们与 `flash_fwd.py` `__init__` 里的属性一一对应。
2. 再确认哪些**运行期**值**不在** `compile_key` 里：`softmax_scale`（数值）、`window_size_left/right`（数值）、输入张量的 `seqlen`、`batch`。你会在键里找到的是 `window_size_left is not None`（[interface.py:738](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L738)）——只有「有没有窗口」这个布尔进键，窗口的具体数值不进。

**需要观察的现象**：

- 「有没有窗口」(`is not None`) 进键 → 会触发特化；但「窗口多大」不进键 → 改窗口数值不会重编译。
- `causal` 是裸布尔进键（[interface.py:725](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L725)）→ 切换 `causal` 必触发重编译。

**预期结果**：你能列出一张「进键 / 不进键」对照表，且与「`self.*` 是否 `StaticTypes`」一致。若无法本地运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：以下哪些参数改变会触发 FA4 前向重编译？(a) `head_dim` 从 64 改到 128；(b) `softmax_scale` 从 `1/8` 改到 `1/√128`；(c) `num_threads` 从 128 改到 384；(d) 把 `q` 的 `seqlen` 从 512 改到 1024。

> **答案**：(a) 会——`head_dim` 进键（[interface.py:722](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L722)）。(c) 会——`num_threads` 进键（[interface.py:749](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L749)）。(b)、(d) 不会——`softmax_scale` 数值与输入形状都不进键。

**练习 2**：为什么 `window_size_left` 的「数值」不进 `compile_key`，而「是否为 `None`」却要进？

> **答案**：滑窗的边界判断最终在 kernel 内是运行期 `Int32` 比较（见 [mask.py:163](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L163) 附近的 `window_size_left`），所以窗口多大不需要特化。但「有没有窗口」决定了 kernel 要不要编译进 `mask_local` 这一整段掩码分支，这会影响生成的指令，必须特化，因此用 `is not None` 进键。

---

### 4.2 score_mod / mask_mod 的 @cute.jit 内联注入

#### 4.2.1 概念说明

`score_mod` 和 `mask_mod` 是用户传进来的**普通 Python 函数**（被 `@cute.jit` 装饰）。它们各自有固定签名：

- `score_mod(score, batch_idx, head_idx, q_idx, kv_idx, seqlen_info, aux_tensors)`：返回修改后的分数。
- `mask_mod(batch_idx, head_idx, q_idx, kv_idx, seqlen_info, aux_tensors)`：返回布尔值，`False` 表示该位置被掩掉。

初学者最自然的疑问是：**kernel 是 GPU 上的机器码，怎么可能「调用」一个 Python 函数？** 答案就是 **编译期内联（inlining）**。CuTeDSL 在编译 kernel 时，会把 `score_mod` / `mask_mod` 的函数体直接「抄写」到调用点，再用编译期常量把坐标算出来，最终这些 Python 运算全部 lowering 成 GPU 指令。编译完成后，运行时根本不存在「函数调用」——它已经变成了一段内联的 GPU 代码。

这有两个直接推论：

1. **`score_mod` / `mask_mod` 必须是 `@cute.jit` 函数**（其函数体只能用 CuTeDSL 支持的算子），不能是任意 Python 函数。它们用 `cutlass.Constexpr` 标注，正是因为「整个函数对象」要在编译期已知，才能被内联。
2. **换一个不同的 `score_mod`（哪怕只改了里面的一个常数），就会生成不同的内联代码**，因此必须触发重编译。

#### 4.2.2 核心流程

score_mod 的注入链：

```text
主循环 compute_one_n_block(...)
   │  算出 acc_S = QK^T（一块分数）
   ▼
if const_expr(score_mod is not None):        # 编译期：有没有 score_mod
    self.apply_score_mod(...)                 # 内联调用
        │
        ▼
apply_score_mod_inner(acc_S, index, score_mod, ...)   # softmax.py
   │  从 acc_S 按 vec_size 抽取 (score, q_idx, kv_idx, batch, head)
   ▼
post_mod_scores = call_score_mod(score_mod, ...)      # softmax.py
   │  内联执行用户函数体 → 返回新分数
   ▼
写回 acc_S
```

mask_mod 的注入链类似，最终落在 `AttentionMask.apply_mask`：

```text
compute_one_n_block(...)
   │
if const_expr(mask_fn is not None):          # 编译期：要不要掩码
    mask_fn(acc_S, n_block=n_block)
        └─ partial(AttentionMask.apply_mask, mask_mod=self.mask_mod, ...)
              │
              ▼
        apply_mask(... mask_mod=..., ...)     # mask.py
           │  const_expr 裁剪：causal / local / mask_mod / 都没有 四选一
           ▼
        call_mask_mod(mask_mod, ...)          # 内联执行用户函数体 → 布尔
           │
           ▼
        acc_S[r,col] = acc_S[r,col] if cond else -inf
```

两条链的共同点：用户函数都以 `cutlass.Constexpr` 形式传入，在编译期被内联。

#### 4.2.3 源码精读

**(a) score_mod 的内联点**

主循环里，`score_mod` 的调用被一个编译期开关包住：

[flash_fwd.py:1154-1166](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1154-L1166)

```python
if const_expr(score_mod is not None):
    self.apply_score_mod(
        mma_params.thr_mma_qk,
        batch_idx, head_idx, m_block, acc_S, n_block,
        softmax_scale=softmax.softmax_scale,
        seqlen=seqlen, aux_data=aux_data, fastdiv_mods=fastdiv_mods,
    )
```

`score_mod` 是 `compute_one_n_block` 的参数（[flash_fwd.py:1102](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1102)，`score_mod: Callable | None`），它实际取自 `self.score_mod`。`const_expr(score_mod is not None)` 在编译期求值：没有 score_mod 时，整段 `apply_score_mod` 调用被删除，生成的 kernel 里根本不存在「修改分数」的指令。

`apply_score_mod` 最终调用公共实现 `apply_score_mod_inner`（[flash_fwd.py:1221](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1221)），后者把用户回调传下去：

[softmax.py:453-469](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L453-L469)

```python
@cute.jit
def apply_score_mod_inner(
    score_tensor, index_tensor,
    score_mod: cutlass.Constexpr,
    batch_idx, head_idx, softmax_scale,
    vec_size: cutlass.Constexpr,
    qk_acc_dtype: cutlass.Constexpr,
    aux_data: AuxData, fastdiv_mods,
    seqlen_info: SeqlenInfoQK,
    constant_q_idx: cutlass.Constexpr,
    qhead_per_kvhead: cutlass.Constexpr[int] = 1,
    transpose_indices: cutlass.Constexpr[bool] = False,
):
```

注意这里几乎所有「会改变生成代码」的参数都是 `cutlass.Constexpr`：`score_mod`、`vec_size`、`qk_acc_dtype`、`constant_q_idx`、`qhead_per_kvhead`、`transpose_indices`。而真正每块都会变的坐标（`batch_idx`、`head_idx`、`softmax_scale`）是运行期标量。函数体的核心是一个按 `vec_size` 展开的循环，在循环里抽取坐标、调用用户函数、写回：

[softmax.py:564-578](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L564-L578)

```python
post_mod_scores = call_score_mod(
    score_mod, score_ssa, batch_idx_ssa, head_idx_ssa,
    q_idx_ssa, kv_idx_ssa, seqlen_info, aux_data,
)
score_vec.store(post_mod_scores)
for j in cutlass.range(vec_size, unroll_full=True):
    score_tensor[i + j] = score_vec[j]
```

真正「调用」用户函数的地方是 `call_score_mod`，它本身也是 `@cute.jit`，参数 `score_mod: cutlass.Constexpr`：

[softmax.py:19-51](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L19-L51)

```python
@cute.jit
def call_score_mod(
    score_mod: cutlass.Constexpr,
    score, batch_idx, head_idx, q_idx, kv_idx, seqlen_info, aux_data: AuxData,
):
    aux_tensors = aux_data.tensors if aux_data.tensors is not None else ()
    if cutlass.const_expr(aux_data.scalars is not None):
        return score_mod(score, batch_idx, head_idx, q_idx=q_idx, kv_idx=kv_idx,
                         seqlen_info=seqlen_info, aux_tensors=aux_tensors,
                         aux_scalars=aux_data.scalars)
    return score_mod(score, batch_idx, head_idx, q_idx=q_idx, kv_idx=kv_idx,
                     seqlen_info=seqlen_info, aux_tensors=aux_tensors)
```

`call_score_mod` 的存在主要是为了**兼容两套签名**（带 `aux_scalars` 与不带，用 `const_expr` 编译期二选一），并给用户提供一个稳定的 8 参数接口。`return score_mod(...)` 这一行就是内联点——编译时，用户 `score_mod` 的函数体会被嵌入到这里。

**(b) mask_mod 的内联点**

mask 路径的结构完全对称。`AttentionMask.apply_mask` 的参数表里，`mask_mod` 也是 `Constexpr`：

[mask.py:185-190](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L185-L190)

```python
mask_seqlen: cutlass.Constexpr[bool],
mask_causal: cutlass.Constexpr[bool],
mask_local: cutlass.Constexpr[bool] = False,
mask_mod: cutlass.Constexpr[Optional[Callable]] = None,
aux_data: AuxData = AuxData(),
fastdiv_mods=(None, None),
```

注意 `mask_causal`、`mask_local`、`mask_mod`、`mask_seqlen` 全是 `Constexpr`。于是函数体开头那串 `if const_expr(...)` 就是**四选一的编译期分发**：

[mask.py:211-226](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L211-L226)

```python
if const_expr(not mask_causal and not mask_local and mask_mod is None):
    # 仅按序列长度做越界掩码（或什么都不做）
    ...
elif const_expr(not mask_causal and not mask_local and mask_mod is not None):
    # FlexAttention mask_mod 分支：逐元素调用用户回调
    ...
else:  # Causal 或 local
    ...
```

编译期只会保留命中条件的那个分支，其余三个被整体删除。这就是为什么「`causal=True` 的 kernel」里看不到 `mask_mod` 的逐元素循环，「带 `mask_mod` 的 kernel」里看不到 R2P 因果掩码指令——它们被特化掉了。

`mask_mod` 分支里，逐元素调用用户回调的内联点：

[mask.py:264-283](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L264-L283)

```python
mask_value = call_mask_mod(
    mask_mod, batch_idx_ssa, head_idx_ssa, q_idx_ssa, kv_idx_ssa,
    self.seqlen_info, aux_data,
)
cond = cutlass.Boolean(utils.ssa_to_scalar(mask_value))
if const_expr(mask_seqlen):
    out_of_bounds = (row_for_seqlen >= self.seqlen_q) or (global_col_idx >= self.seqlen_k)
    if out_of_bounds:
        acc_S_mn[r, col] = -cutlass.Float32.inf
    else:
        acc_S_mn[r, col] = acc_S_mn[r, col] if cond else -cutlass.Float32.inf
else:
    acc_S_mn[r, col] = acc_S_mn[r, col] if cond else -cutlass.Float32.inf
```

`call_mask_mod`（[mask.py:22-50](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L22-L50)）与 `call_score_mod` 同构：`mask_mod: cutlass.Constexpr`，`return mask_mod(...)` 即内联点。最终 `acc_S_mn[r, col] if cond else -inf` 把布尔掩码 lowering 成「条件赋 `-inf`」，这正是 softmax 前把非法位置清零的标准做法。

#### 4.2.4 代码实践

**实践目标**：理解 `softcap=` 参数是如何被翻译成一个内置 `score_mod` 并内联进 kernel 的。

**操作步骤**（源码阅读型）：

1. 读 `interface.py` 里 softcap 到 score_mod 的转换 [interface.py:607-612](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L607-L612)：用户传 `softcap=50.0` 时，`score_mod` 被设为 `utils.create_softcap_scoremod(softcap)`，并 `assert score_mod is None`（二者互斥）。
2. 读 `create_softcap_scoremod` 的定义 [utils.py:159-167](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L159-L167)：它返回一个 `@cute.jit` 函数，函数体是 \(\text{score} \mapsto c \cdot \tanh(\text{score}/c)\)。
3. 把这条链串起来：`softcap=50.0` → `create_softcap_scoremod(50.0)` → 一个闭包捕获了 `softcap_val=50.0` 的 `@cute.jit` 函数 → 作为 `score_mod: cutlass.Constexpr` 内联进 `call_score_mod` → 编译成 GPU 指令。

**需要观察的现象**：`softcap_val` 是闭包捕获值。改 `softcap=50.0` 为 `softcap=30.0`，会生成一个**不同的**闭包函数对象，进而得到不同的源码哈希（见 4.3）。

**预期结果**：你能解释「`softcap` 的数值变化为何会触发重编译」——因为该数值被烘焙进了内联的 score_mod 函数体，函数源码（含捕获值）变了。若需运行验证，可参考 4.3.4 的计时实验。

#### 4.2.5 小练习与答案

**练习 1**：假如有人想用一个**普通 Python 函数**（没有 `@cute.jit`、内部用了 `numpy`）作为 `score_mod`，会发生什么？

> **答案**：无法工作。`score_mod` 必须是 `@cute.jit` 函数，其函数体只能用 CuTeDSL 支持的算子，才能在编译期被内联 lowering 成 GPU 指令。普通 Python 函数（尤其用了 `numpy`）无法被 CuTeDSL 编译，会在 `cute.compile` 阶段报错。

**练习 2**：`call_score_mod` 里那段 `if cutlass.const_expr(aux_data.scalars is not None):` 的意义是什么？为什么不用普通 `if`？

> **答案**：它用编译期判断在「带 `aux_scalars`」与「不带」两套签名间二选一。若用普通 `if`，两条分支都会被编译进 kernel，运行时还要判断；而 `const_expr` 让编译期只保留命中的一条，既避免冗余代码，也让 `score_mod(...)` 的调用签名在编译期确定。

---

### 4.3 特化与重编译：compile_key 的构成与触发条件

#### 4.3.1 概念说明

前两节建立了两条产生特化的来源：`self.*` 上的 Python 常量，与 `score_mod`/`mask_mod` 这类 `Constexpr` 回调。但 kernel 自己不会记录「我特化成了几份」——这件事由宿主侧的 `compile_key` 统一管理。`compile_key` 是一个**普通 Python 元组**，它的相等性就是「是否需要重新编译」的判据（详见 u11-l1）：

[interface.py:769](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L769)

```python
if compile_key not in _flash_attn_fwd.compile_cache:
    ...
    _flash_attn_fwd.compile_cache[compile_key] = cute.compile(*compile_args, ...)
```

本节要回答的工程问题是：**`compile_key` 的每一项，与「代码生成」之间是什么对应关系？** 也就是把「特化」（kernel 视角）与「重编译」（缓存视角）对齐。

#### 4.3.2 核心流程

构造 `compile_key` 的字段可按「来源」分三类：

```text
compile_key = (
    # ① 来自 self.* 的 Python 常量（Constexpr 语义）
    dtype, head_dim, head_dim_v, qhead_per_kvhead, causal,
    use_block_sparsity, ..., tile_m, tile_n, q_stage, num_threads,
    is_split_kv, pack_gqa, arch, use_2cta_instrs, q_subtile_factor,
    mma_pv_is_rs, intra_wg_overlap, use_clc_scheduler, ...,

    # ② score_mod / mask_mod 的「源码哈希」（它们本身是 Constexpr 回调）
    score_mod_hash, mask_mod_hash,

    # ③ 运行期对象的「是否存在」(is None / is not None)：
    #    这些布尔决定 kernel 要不要编译进某段分支
    lse is None, cu_seqlens_q is None, ..., page_table is not None,
    window_size_left is not None, ..., fa_logging.get_fa_log_level(),
)
```

判等规则：**只要有一项变了，就视为新的 compile_key → 缓存未命中 → `cute.compile` 重编译**。这恰好与 4.1、4.2 里「哪些值会改变生成代码」完全吻合：凡会改变生成代码的，都进了键；凡只改运行时数值、不改生成代码的（如 `softmax_scale` 数值、输入形状），都不进键。

#### 4.3.3 源码精读

**(a) score_mod / mask_mod 的哈希如何进键**

回调和「简单常量」不同：它是一个函数对象，不能直接放进元组当键（函数对象默认按 `id` 比较相等，不可靠）。所以先求它的**源码哈希**：

[interface.py:614-616](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L614-L616)

```python
# hash score and mask mods for compile cache
score_mod_hash = utils.hash_callable(score_mod) if score_mod is not None else False
mask_mod_hash = utils.hash_callable(mask_mod) if mask_mod is not None else False
```

没有回调时用哨兵 `False`（一个与任何哈希字符串都不等的值），有回调时用 `hash_callable`。这两个哈希随后放进键：

[interface.py:725-728](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L725-L728)

```python
causal,
score_mod_hash,
mask_mod_hash,
use_block_sparsity,
```

`hash_callable` 的核心是对函数源码求 SHA-256，并把闭包捕获值也混进去：

[utils.py:102-118](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L102-L118)

```python
def _compute_base_hash(func: Callable) -> str:
    """Compute hash from source code or bytecode and closure values."""
    try:
        data = inspect.getsource(func).encode()
    except (OSError, TypeError):
        ...
    hasher = hashlib.sha256(data)
    if hasattr(func, "__closure__") and func.__closure__ is not None:
        for cell in func.__closure__:
            hasher.update(repr(cell.cell_contents).encode())
    return hasher.hexdigest()
```

关键点：哈希同时覆盖**源码文本**与**闭包捕获值**。这正是 4.2.4 里「`softcap` 从 50 改到 30 会重编译」的根因——`softcap_val` 是闭包捕获值，`repr(50.0)` 与 `repr(30.0)` 不同，哈希不同，`score_mod_hash` 这一项变了，`compile_key` 不等 → 重编译。

`hash_callable` 还会把 `__vec_size__` 这类「混入属性」一起哈希（[utils.py:146-156](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L146-L156)），所以「换一个不同向量化宽度的 score_mod」也会改哈希。

**(b) 为什么 `causal` 进键、`softmax_scale` 不进键**

回到键的开头：

[interface.py:720-725](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L720-L725)

```python
compile_key = (
    dtype,
    head_dim,
    head_dim_v,
    qhead_per_kvhead,
    causal,
    ...
```

`causal` 直接以裸布尔进键。它与 `mask.py` 里 `mask_causal: cutlass.Constexpr[bool]`（[mask.py:186](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L186)）和 `BlockInfo.is_causal`（编译期）一脉相承：`causal` 决定 kernel 要不要编译进因果掩码分支（`mask.py` 的 `else: # Causal or local` 分支），所以必须特化。

而 `softmax_scale`（运行期 `Float32`，[flash_fwd.py:763](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L763)）只是参与乘法运算，不改变控制流，因此不进键。注意它有一个精妙之处（见 u4-l1、[utils.py:185-197](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L185-L197)）：有没有 `score_mod` 会改变 `softmax_scale` 的「身份」（折叠进 `scale_log2` 还是单独传给回调），但这个「身份」已经由 `score_mod_hash` 进键间接表达了，所以 `softmax_scale` 的数值本身仍无需进键。

**(c) `pack_gqa` 进键**

[interface.py:751](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L751) 把 `pack_gqa` 放进键。它与 `flash_fwd.py` 里成片的 `const_expr(self.pack_gqa)` 对应（4.1.3 (b)）：开/关 pack_gqa 会改变 Q 的加载路径（`PackGQA.load_Q` vs 普通 cp.async）、输出路径（`store_O`/`store_LSE`）与 num_head_kv 的计算，这些都改变生成代码，故必须特化。

**(d) 缓存的归属**

最后，内存缓存的容器本身也在 `interface.py` 里初始化：

[interface.py:1104](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1104)

```python
_flash_attn_fwd.compile_cache = get_jit_cache("fwd")
```

`get_jit_cache("fwd")` 返回进程内缓存（可选叠加磁盘缓存，由 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED` 控制，见 u11-l1）。`compile_key` 就是这个 dict 的键。

#### 4.3.4 代码实践

**实践目标**：用计时与缓存命中，亲手观察「切换 `causal` / `pack_gqa` 是否触发重编译」，并用本讲知识解释。

**操作步骤**（需 GPU；若无 GPU 可用 FakeTensor 模式只观察编译，见末尾说明）：

1. 新建脚本，关掉磁盘缓存以便观察「首次编译」耗时：
   ```bash
   export FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=0
   python my_experiment.py
   ```
2. 脚本主体（示例代码，非项目原有）：
   ```python
   import time, torch
   from flash_attn.cute import flash_attn_func
   from flash_attn.cute.interface import _flash_attn_fwd

   def run(causal, pack_gqa, warm=False):
       q = torch.randn(1, 512, 8, 64, dtype=torch.float16, device="cuda")
       k = torch.randn(1, 512, 2, 64, dtype=torch.float16, device="cuda")  # GQA: 8 vs 2
       v = torch.randn_like(k)
       cache_size_before = len(_flash_attn_fwd.compile_cache)
       t0 = time.perf_counter()
       out, lse = flash_attn_func(q, k, v, causal=causal, pack_gqa=pack_gqa)
       torch.cuda.synchronize()
       dt = time.perf_counter() - t0
       cache_size_after = len(_flash_attn_fwd.compile_cache)
       compiled = cache_size_after > cache_size_before
       print(f"causal={causal}, pack_gqa={pack_gqa}: {dt:.3f}s, "
             f"cache {cache_size_before}->{cache_size_after}, "
             f"{'RECOMPILED' if compiled else 'cache hit'}")

   run(False, True)   # 第一次：编译
   run(False, True)   # 同 key：命中缓存，应明显更快
   run(True,  True)   # causal 变 → 重编译
   run(True,  True)   # 命中
   run(True,  False)  # pack_gqa 变 → 重编译
   ```

**需要观察的现象**：

- 第 1、3、5 次调用显著慢于第 2、4 次（首次编译耗时通常以秒计，命中缓存以毫秒计）。
- 缓存大小在第 1、3、5 次各 +1，第 2、4 次不变。

**预期结果**：切换 `causal` 触发重编译（`causal` 进键、改变掩码分支）；切换 `pack_gqa` 同样触发重编译（`pack_gqa` 进键、改变 Q 加载/输出路径）。

**无 GPU 环境的替代观察**（待本地验证）：用 FakeTensor 模式只跑编译、不跑 kernel，可观察到同样的「缓存增长」：
```bash
FLASH_ATTENTION_FAKE_TENSOR=1 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=0 \
  python my_experiment.py
```
此时每次 `flash_attn_func` 只编译不执行，`compile_cache` 增长模式应与有 GPU 时一致。注意：`len()` 依赖于 `get_jit_cache` 返回对象支持 `__len__`，若报错可改用 `compile_key in _flash_attn_fwd.compile_cache` 的方式手动判等。

#### 4.3.5 小练习与答案

**练习 1**：`head_dim`、`causal`、`score_mod`、`softmax_scale` 这四项，哪几个进 `compile_key`？分别以什么形式进？

> **答案**：前三项进键。`head_dim` 以裸整数进（[interface.py:722](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L722)）；`causal` 以裸布尔进（[interface.py:725](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L725)）；`score_mod` 以**源码哈希** `score_mod_hash` 进（[interface.py:726](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L726)）。`softmax_scale` 的数值不进键。

**练习 2**：用户写了一个 ALiBi 的 `score_mod`，把它内部的一个斜率常数从 `0.1` 改成 `0.2`，会触发重编译吗？为什么？

> **答案**：会。该斜率是闭包捕获值，`hash_callable` → `_compute_base_hash` 会把 `repr(闭包值)` 混进哈希（[utils.py:114-116](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L114-L116)），`score_mod_hash` 改变 → `compile_key` 不等 → 重编译。这正是「回调里的常数也算编译期常量」的体现。

**练习 3**：为什么 FA4 不把 `softmax_scale` 也设成 `Constexpr`、让它特化？

> **答案**：特化的代价是一次性编译开销与缓存膨胀。`softmax_scale` 只是一个乘法，不改变控制流，把它设成运行期 `Float32` 既能支持「每次调用换不同 scale」而无需重编译，又零性能损失。CuTeDSL 的设计哲学是：**只对会改变生成代码（控制流、tile 形状、类型）的量做特化**，纯数值量留作运行期参数。

---

## 5. 综合实践

把三个最小模块串成一个完整任务：**给 FA4 前向画一张「参数 → 特化 → 重编译」的因果图**。

任务步骤：

1. 选定一组基线配置：`dtype=fp16, head_dim=64, causal=False, pack_gqa=True, num_heads=8, nheads_kv=2, seqlen=512`。
2. 逐一改变下列每个参数（每次只改一个），用 4.3.4 的计时 + 缓存大小法判断是否触发重编译，并填入下表：

   | 改动的参数 | 是否重编译 | 进键的字段 | 代码生成受影响的处 |
   | --- | --- | --- | --- |
   | `causal: False→True` | ? | ? | ? |
   | `head_dim: 64→128` | ? | ? | ? |
   | `pack_gqa: True→False` | ? | ? | ? |
   | `softmax_scale: 0.125→0.088` | ? | （不进键） | （无） |
   | `softcap: None→50.0` | ? | ? | ? |
   | `softcap: 50.0→30.0` | ? | ? | ? |
   | `window_size_left: None→128` | ? | ? | ? |
   | `window_size_left: 128→256` | ? | ? | ? |

3. 对每一行，在源码里找到**受影响的代码生成处**并给出永久链接。例如 `causal` 行应指向 [mask.py:186](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L186) 的 `mask_causal: cutlass.Constexpr[bool]` 与 [mask.py:285](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L285) 的 `else: # Causal or local` 分支；`softcap` 行应指向 [interface.py:607-609](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L607-L609) 与 [utils.py:159-167](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L159-L167)。

4. **用一句话总结规律**：什么样的参数变化会触发重编译？（参考答案：凡改变 kernel 控制流、tile/类型/线程配置，或改变被内联回调的源码/闭包值的，都会；纯运行期数值与输入形状不会。）

> 说明：综合实践以源码阅读 + 计时观察为主。若环境无 GPU，可用 `FLASH_ATTENTION_FAKE_TENSOR=1` 只观察缓存增长（待本地验证）。

---

## 6. 本讲小结

- FA4 用两类机制把值变成「编译期常量」：显式 `cutlass.Constexpr` 参数注解，以及通过 `self.*` 捕获进 `@cute.kernel`/`@cute.jit` 的 Python `int`/`bool`（后者由 `StaticTypes` 界定）。
- `const_expr(x)` 是函数体里的「编译期求值」工具，`if const_expr(...)` 让无用分支在编译期被删除，从而生成更精简的特化 kernel。
- `score_mod` / `mask_mod` 是 `@cute.jit` 回调，以 `Constexpr` 形式在编译期被**内联**进 kernel（经 `call_score_mod` / `call_mask_mod`），运行时不存在函数调用；这正是它们不能用普通 Python 函数的原因。
- 宿主侧用 `compile_key` 元组统一管理特化：`causal`、`head_dim`、`tile_m/n`、`pack_gqa`、`arch` 等以裸值进键，`score_mod`/`mask_mod` 以**源码哈希**（含闭包值）进键；键不等即重编译。
- 判定口诀：**改变生成代码（控制流、tile/类型/线程、被内联回调的源码或闭包值）→ 特化 → 重编译；纯运行期数值（如 `softmax_scale`、窗口大小数值）与输入形状 → 不特化 → 不重编译。**

---

## 7. 下一步学习建议

- 想看「特化产物」长什么样，可读 [u11-l5 GPU Kernel 调试与 PTX/SASS](u11-l5-debugging-ptx-sass.md)：用 `CUTE_DSL_KEEP_PTX=1` 导出 PTX，对比 `causal=True/False` 两份 PTX 的差异，亲眼看到被 `const_expr` 删掉的分支。
- 想理解「特化如何被测试覆盖」，可读 [u11-l3 测试体系与参考实现](u11-l3-tests-and-reference.md)：测试参数化维度（dtype、head_dim、causal、GQA/MQA）本质上就是在遍历 `compile_key` 的关键组合。
- 想深入 score_mod / mask_mod 的更多实例（ALiBi、相对位置偏置、文档级掩码），可回顾 [u4-l2 score_mod](u4-l2-score-mod.md) 与 [u3-l1 AttentionMask](u3-l1-attention-mask.md)，并用本讲的「源码哈希」视角重新理解「为何换一个偏置函数就要重编译」。
- 继续往下，可阅读 `flash_attn/cute/cute_dsl_utils.py` 里被 patch 过的 `cute.compile`，理解 CuTeDSL 如何在编译时把 `StaticTypes` 参数与运行期参数分流，这是本讲「特化」机制的更底层实现。
