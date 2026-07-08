# gen_*_module 代码生成模式

## 1. 本讲目标

本讲是「JIT 编译系统」单元的第三讲。在 u2-l1 里我们建立了三层架构的总纲（定义层 → 代码生成层 → 编译加载层），在 u2-l2 里我们看清了 `JitSpec` 和工作区路径。本讲要钻进**第二层（代码生成层）**，搞清楚一个具体问题：

> 当用户调用某个算子时，FlashInfer 是怎样「凭空」生成一份专属的 `.cu` 源码、再交给编译器去编译的？

读完本讲你应当能够：

1. 说出 `gen_*_module` 这一类函数的**标准五步流程**，并能对照真实代码指出每一步。
2. 理解**模块名 / URI** 是如何由参数算出来的，为什么 activation 的「URI」只是一个普通名字、而 attention 的 URI 是一长串描述性字符串。
3. 理解 **Jinja 渲染**如何对 C++ 模板做「类型 / 参数特化」，把抽象占位符替换成具体类型。
4. 区分两种产生源文件的方式：**直接生成整份 `.cu`**（activation）与 **从 `CSRC_DIR` 拷贝现有 `.cu`**（norm / attention）。
5. 看懂最简单的生成器 `gen_act_and_mul_module`，并能动手渲染/新增一个激活函数。

本讲的范例是「激活函数」——它是整个项目里最简单的 `gen_*_module`，足以让我们看清代码生成的骨架，而不被注意力那套复杂的参数空间干扰。

## 2. 前置知识

### 2.1 什么是「代码生成（codegen）」

C++ 模板（template）有一个老问题：`__nv_bfloat16`、`half`、`head_dim=128`、`head_dim=256` …… 这些参数的组合会爆炸式增长。如果**提前（AOT）**把每一种组合都编译成 `.so`，分发体积会大到不可接受；但如果只用一套通用代码，运行期又会被分支判断拖慢。

FlashInfer 的折中是 **JIT（即时编译）**：等到真正知道「这次调用要 bf16、head_dim=128、用 silu」之后，再现场**生成一份只针对这一组参数的 `.cu` 文件**，编译成专属 `.so`。代码生成层负责的，就是「生成这份 `.cu`」。

### 2.2 Jinja 模板引擎（极简版）

[Jinja2](https://jinja.palletsprojects.com/) 是 Python 里最常见的模板引擎，和 Django/Flask 的网页模板同源。它的核心就两件事：

- `{{ 变量名 }}`：把 Python 变量的值**字符串拼接**进模板。
- `{% 语句 %}`：在模板里写控制流（循环、设变量）。

例如模板：

```jinja
using DType = {{ dtype }};
constexpr int K = {{ head_dim }};
```

当 Python 传入 `dtype="half"`、`head_dim=128` 渲染后，就得到：

```cpp
using DType = half;
constexpr int K = 128;
```

本讲你会看到 FlashInfer 用同一套机制，把 `half`、`silu` 这样的具体名字织进 CUDA 代码。

### 2.3 回顾：三层架构与本讲位置

| 层 | 角色 | 关键产物 |
|----|------|---------|
| 定义层 | `JitSpec` 数据类 + `gen_jit_spec` | 一张「编译配料单」 |
| **代码生成层（本讲）** | 各 `gen_*_module` 函数 | 一份份具体的 `.cu` 源码 |
| 编译加载层 | `build` / `build_and_load` | 编译出 `.so` 并加载回 Python |

本讲只聚焦中间一层：它**输入**是一组编译期参数（dtype、激活名、head_dim 等），**输出**是一个 `JitSpec`（里面带上了刚生成的 `.cu` 路径）。

### 2.4 术语速查

- **URI**：这里指模块的「唯一名字字符串」，由参数拼成，决定生成的源文件叫什么、编译产物 `.so` 叫什么。它**不是** URL。
- **特化（specialization）**：把通用模板固化成某一组具体参数的代码。
- **生成目录（gen directory）**：写出生成源码的目录，位于可写区 `FLASHINFER_GEN_SRC_DIR`。
- **`CSRC_DIR`**：仓库里只读的 `csrc/` 模板目录（安装后映射到 `flashinfer/data/csrc`）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [flashinfer/jit/activation.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py) | **主角**。最简单的生成器，定义了激活函数的 Jinja 模板与 `gen_act_and_mul_module`。 |
| [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | 提供 `gen_jit_spec` 装配 `JitSpec`、并把它登记进全局注册表。 |
| [flashinfer/jit/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py) | 提供 `write_if_different`（幂等写文件）与 dtype/posenc 等查找表。 |
| [flashinfer/jit/norm.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/norm.py) | **对照样本**。「只拷贝、不渲染」的最简生成器。 |
| [flashinfer/jit/attention/modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py) | **对照样本**。完整的五步流程（含 URI 子目录 + Jinja 配置 + 拷贝源文件）。 |
| [flashinfer/activation.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/activation.py) | 生成器的**消费方**：Python API 调 `gen_act_and_mul_module(...).build_and_load()`。 |

学习策略：以 `activation.py` 为主线讲清骨架，用 `norm.py`（极简）和 `attention/modules.py`（完整）做两个对照样本，三角定位「五步流程」。

## 4. 核心概念与源码讲解

### 4.1 模块名 / URI 的计算

#### 4.1.1 概念说明

每一个会被 JIT 编译的 CUDA 模块，都需要一个**全局唯一的名字**。这个名字同时承担三件大事：

1. 决定生成出来的源文件叫什么（`silu_and_mul.cu`）。
2. 决定编译产物 `.so` 叫什么、放在哪个子目录（`cached_ops/silu_and_mul/silu_and_mul.so`）。
3. 充当**缓存键**——只要名字相同，就复用之前编译好的 `.so`。

FlashInfer 把这个名字称为 **URI**（在 CLAUDE.md 里描述为「unique identifier」）。它的关键设计是：**URI 只编码「编译期参数」，不编码「运行期形状」**。例如 `silu` vs `gelu` 是不同的激活、必须各自编译，所以 `silu` 和 `gelu` 的 URI 不同；但 batch size、序列长度这些运行期量不进 URI（否则每换一个 batch 都要重新编译）。

#### 4.1.2 核心流程

FlashInfer 里 URI 的计算分两个极端，中间还有连续的过渡：

- **最简（activation）**：唯一的参数就是激活函数的名字，那么**名字本身就是 URI**。`silu` 的 URI 直接是字符串 `silu_and_mul`。不需要任何拼接或哈希。
- **复合（attention）**：参数很多（dtype_q、dtype_kv、head_dim、posenc……），把这些参数**拼成一段人类可读的描述性字符串**当 URI。

两种做法的目的相同：把「会影响编译结果的参数」唯一地编码进一个字符串。activation 因为参数太少，退化成了「直接用名字」。

```
参数 (act_func_name="silu")
        │  退化：名字即 URI
        ▼
URI = "silu_and_mul"
        │
        ▼
源文件: generated/silu_and_mul.cu
产物:   cached_ops/silu_and_mul/silu_and_mul.so
```

#### 4.1.3 源码精读

**activation：名字即 URI。** 在 [flashinfer/jit/activation.py:105-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L105-L117) 中，`gen_act_and_mul_module` 把 URI 直接拼成 `f"{act_func_name}_and_mul"`，再原样传给 `gen_jit_spec`：

```python
def gen_act_and_mul_module(act_func_name: str) -> JitSpec:
    act_func_def = act_func_def_str[act_func_name]
    ...
    return gen_jit_spec(
        f"{act_func_name}_and_mul",   # ← 这就是 URI：silu_and_mul / gelu_and_mul / ...
        sources,
    )
```

所以传入 `"silu"` → URI 是 `silu_and_mul`；传入 `"gelu_tanh"` → URI 是 `gelu_tanh_and_mul`。没有任何哈希、没有任何 `get_*_uri` 辅助函数。

**attention：复合描述性 URI。** 作为对照，看 [flashinfer/jit/attention/modules.py:45-64](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L45-L64)，`get_single_decode_uri` 把 8 个编译期参数拼成一长串：

```python
def get_single_decode_uri(dtype_q, dtype_kv, dtype_o, head_dim_qk,
                          head_dim_vo, pos_encoding_mode,
                          use_sliding_window, use_logits_soft_cap) -> str:
    return (
        f"single_decode_with_kv_cache_dtype_q_{filename_safe_dtype_map[dtype_q]}_"
        f"dtype_kv_{filename_safe_dtype_map[dtype_kv]}_"
        ...
        f"head_dim_qk_{head_dim_qk}_"
        ...
    )
```

其中 `filename_safe_dtype_map`（[flashinfer/jit/utils.py:69-82](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py#L69-L82)）把 `torch.float16` 映射成文件名安全的短串 `f16`、`torch.bfloat16` → `bf16`。最终 URI 形如 `single_decode_with_kv_cache_dtype_q_f16_dtype_kv_f16_..._head_dim_qk_128_...`。

> 设计要点：URI 用**可读字符串**而不是哈希，是为了让人一眼看出「这个 `.so` 是给哪组参数编译的」，方便调试和缓存管理。CLAUDE.md 里提到的「hash」是对**源码内容**的哈希（用于检测源码改动后失效），它与这里的「URI 字符串」是两回事——别混淆。

#### 4.1.4 代码实践

**实践目标**：直观感受「名字即 URI」与「复合 URI」的差异。

**操作步骤**（无需 GPU，纯源码阅读）：

1. 打开 [flashinfer/jit/activation.py:98-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L98-L117)，确认 `act_func_def_str` 里有 `silu` / `gelu` / `gelu_tanh` 三个键，因此存在三个 URI：`silu_and_mul`、`gelu_and_mul`、`gelu_tanh_and_mul`。
2. 打开 [flashinfer/jit/attention/modules.py:67-88](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L67-L88)，数一下 `get_batch_decode_uri` 用了几个参数拼 URI。
3. 假设你要为一个 `bf16`、`head_dim_qk=128`、`head_dim_vo=128`、`posenc=0` 的 decode 编译模块，在脑海里把 `filename_safe_dtype_map[torch.bfloat16]`（即 `bf16`）代入，拼出大致的 URI 前缀。

**需要观察的现象**：

- activation 的 URI 短到只是一个普通标识符；attention 的 URI 长到像一句话，把所有编译期参数都「读」了出来。

**预期结果**：

- 三个 activation URI：`silu_and_mul`、`gelu_and_mul`、`gelu_tanh_and_mul`。
- `get_batch_decode_uri` 用了 9 个参数（多了一个 `dtype_idx`）。
- batch_decode URI 前缀形如 `batch_decode_with_kv_cache_dtype_q_bf16_dtype_kv_bf16_...`。

#### 4.1.5 小练习与答案

**练习 1**：如果用户先用 `silu`、再用 `gelu` 各调用一次激活，会编译出几个 `.so`？为什么？

**答案**：两个。因为 URI 分别是 `silu_and_mul` 与 `gelu_and_mul`，名字不同 → 各自一份 `.so`。这正是「URI 是缓存键」的体现：参数变了就要重新编译。

**练习 2**：为什么 attention 的 URI 里没有 `batch_size`、`seq_len` 这样的运行期形状？

**答案**：因为它们不影响**编译结果**——同一份 kernel 代码可以处理任意 batch/seq。把它们写进 URI 会导致每换一组形状就重新编译，缓存形同虚设。URI 只收录「会改变生成的代码本身」的编译期参数。

---

### 4.2 Jinja 渲染：做参数特化

#### 4.2.1 概念说明

算出 URI 只是起了个名字，还没产生任何代码。**Jinja 渲染**才是「把通用模板固化成专属源码」的环节。

activation 的做法很特别：它把**整份 `.cu` 文件**都写成一个 Jinja 模板。模板里留两个「洞」：

- `{{ act_func_def }}`：激活函数的 C++ 定义（`silu` / `gelu` / `gelu_tanh` 各一份）。
- `{{ act_func_name }}`：激活函数的名字，用来拼出导出符号 `silu_and_mul` 和模板实参 `act_and_mul_kernel<c_type, silu>`。

渲染时把这两个洞填上具体内容，就得到了一份完整、可编译的 `silu_and_mul.cu`。这种方式的好处：**一份模板服务所有激活函数**，新增一种激活只需加一段 C++ 片段，完全不用碰 kernel 主体。

#### 4.2.2 核心流程

```
act_func_name="silu"
        │
        ├──► 查 act_func_def_str["silu"] = silu_def_cu_str（一段 C++）
        │
        ▼
   jinja2.Template(activation_templ)
        │  .render(act_func_name="silu", act_func_def=silu_def_cu_str)
        ▼
   完整的 silu_and_mul.cu 文本
```

渲染发生的两处关键替换：

1. `{% set func_name = act_func_name ~ '_and_mul' %}` → `func_name = "silu_and_mul"`，随后 `void {{ func_name }}(...)` 变成 `void silu_and_mul(...)`。
2. `{{ act_func_def }}` 被替换成 `silu` 的 `__device__` 定义；`act_and_mul_kernel<c_type, {{ act_func_name }}>` 变成 `act_and_mul_kernel<c_type, silu>`——这里把**激活函数当成模板参数**传进 kernel，是 C++ 模板 + Jinja 的双重特化。

#### 4.2.3 源码精读

**模板本体**在 [flashinfer/jit/activation.py:25-69](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L25-L69)。注意其中三处 Jinja 占位符：

```jinja
{% set func_name = act_func_name ~ '_and_mul' %}

{{ act_func_def }}                      {# ← 注入激活函数的 C++ 定义 #}

void {{ func_name }}(TensorView out, TensorView input, bool enable_pdl) {
  ...
  auto kernel = flashinfer::activation::act_and_mul_kernel<c_type, {{ act_func_name }}>;
  ...
}

TVM_FFI_DLL_EXPORT_TYPED_FUNC({{ func_name }}, {{ func_name }});  {# ← 导出符号 #}
```

**三种激活的 C++ 片段**在 [flashinfer/jit/activation.py:77-102](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L77-L102)，例如 `silu`：

```cpp
__device__ __forceinline__ float silu(const float& val) {
  return val / (1.0f + __expf(-val));
}
```

它们被收进字典 `act_func_def_str = {"silu": ..., "gelu": ..., "gelu_tanh": ...}`。

**渲染函数**只有三行，见 [flashinfer/jit/activation.py:72-74](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L72-L74)：

```python
def get_act_and_mul_cu_str(act_func_name: str, act_func_def: str) -> str:
    template = jinja2.Template(activation_templ)
    return template.render(act_func_name=act_func_name, act_func_def=act_func_def)
```

> 对照样本：attention 并不把整份 `.cu` 写成 Jinja，而是只渲染一个**很小的配置片段** `batch_mla_config.inc`（[flashinfer/jit/attention/modules.py:138-151](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L138-L151)），里面只放 `using DTypeQ = ...;` 这类 typedef，真正的 kernel 逻辑放在被拷贝的 `.cu` 里 `#include` 这个 `.inc`。两种风格（整文件渲染 vs 小配置渲染）只是粒度不同，本质都是「用 Jinja 做类型特化」。

#### 4.2.4 代码实践

**实践目标**：亲手渲染一份 `.cu`，看清 Jinja 到底替换了什么。这一步**不需要 GPU、不需要编译**，纯字符串操作。

**操作步骤**：在仓库根目录运行下面的脚本（示例代码）：

```python
# 示例代码：渲染并对比两个激活的生成结果
from flashinfer.jit import get_act_and_mul_cu_str
from flashinfer.jit.activation import silu_def_cu_str, gelu_def_cu_str

silu_cu = get_act_and_mul_cu_str("silu", silu_def_cu_str)
gelu_cu = get_act_and_mul_cu_str("gelu", gelu_def_cu_str)

print(silu_cu[:200])          # 看前 200 字符
print("导出符号:", "TVM_FFI_DLL_EXPORT_TYPED_FUNC(silu_and_mul" in silu_cu)
print("模板实参:", "act_and_mul_kernel<c_type, silu>" in silu_cu)
print("gelu 实参:", "act_and_mul_kernel<c_type, gelu>" in gelu_cu)
```

**需要观察的现象**：

- `silu_cu` 中函数名是 `silu_and_mul`，`gelu_cu` 中是 `gelu_and_mul`——同一份模板，名字不同。
- `silu_cu` 含 `act_and_mul_kernel<c_type, silu>`；`gelu_cu` 含 `act_and_mul_kernel<c_type, gelu>`——激活函数作为模板参数被特化进去了。
- 两者除「名字 + 激活定义片段」外其余完全一致，说明 kernel 主体是共享的。

**预期结果**：三个 `print` 依次输出一段以 `#include <flashinfer/activation.cuh>` 开头的 C++、`True`、`True`、`True`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 activation 选择「把整份 `.cu` 写成 Jinja」，而 attention 选择「只渲染一个小 `.inc`」？

**答案**：activation 的差异面极小——只有激活函数本身不同，主体逻辑完全共享，所以干脆把整份文件参数化，最简洁。attention 的参数空间大、kernel 主体复杂且依赖大量 CUTLASS 头文件，把整份 `.cu` 参数化会很难维护；只渲染一个小配置头、让主体 `.cu` 通过 `#include` 读它，是更可控的做法。这是「差异面大小」决定的工程取舍。

**练习 2**：模板里 `act_and_mul_kernel<c_type, {{ act_func_name }}>` 把激活函数当模板参数传入。如果 `silu_def_cu_str` 里定义的函数名不叫 `silu`（比如笔误成 `silu_x`），渲染还能成功吗？编译呢？

**答案**：渲染会成功——Jinja 只做字符串替换，不检查 C++ 语义。但编译会失败：`act_and_mul_kernel<c_type, silu>` 会找一个名叫 `silu` 的可调用对象，而实际定义的是 `silu_x`，链接/模板实例化时报未定义符号。这说明 **Jinja 只负责生成文本，正确性仍由 C++ 编译器把关**。

---

### 4.3 源文件的产生：直接生成 vs 从 CSRC_DIR 拷贝

#### 4.3.1 概念说明

渲染得到 `.cu` 文本之后，得把它**落到磁盘上**才能交给 nvcc。FlashInfer 有两种产生源文件的路径，activation 与 norm 正好各代表一极：

- **直接生成**（activation）：`.cu` 内容是 Jinja 渲染出来的，原本磁盘上不存在，写到一个新文件。
- **拷贝现有文件**（norm / attention）：`.cu` 早就在只读的 `CSRC_DIR` 里写好了，生成器把它「搬」到可写的生成目录（或干脆原地引用）。

无论哪种，落盘都经过同一个工具函数 `write_if_different`——它保证**内容没变就不写**，避免无谓地刷新文件修改时间、触发 ninja 重新编译。

#### 4.3.2 核心流程

```
                ┌─────────────────────────────────────────────┐
两种产生方式     │  直接生成（activation）                      │
                │    get_act_and_mul_cu_str(...) → 文本        │
                │              │ write_if_different            │
                │              ▼                               │
                │    generated/silu_and_mul.cu                 │
                ├─────────────────────────────────────────────┤
                │  拷贝（norm）                                 │
                │    CSRC_DIR/norm.cu ──read──► 文本            │
                │              │ （norm 直接把 CSRC_DIR 路径    │
                │              │   当 sources，连拷贝都省了）    │
                │              ▼                               │
                │    sources = [CSRC_DIR/norm.cu, ...]         │
                └─────────────────────────────────────────────┘
```

`write_if_different` 的幂等逻辑（[flashinfer/jit/utils.py:22-30](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py#L22-L30)）：先读旧文件，若内容与待写内容完全相同就**直接 return**；只有不同才落盘。这样每次 `import flashinfer` 都跑一遍代码生成，也不会反复触发重编译。

#### 4.3.3 源码精读

**activation：生成并落盘。** [flashinfer/jit/activation.py:105-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L105-L117)：

```python
def gen_act_and_mul_module(act_func_name: str) -> JitSpec:
    act_func_def = act_func_def_str[act_func_name]
    gen_directory = jit_env.FLASHINFER_GEN_SRC_DIR        # 可写区：generated/
    os.makedirs(gen_directory, exist_ok=True)
    sources = [gen_directory / f"{act_func_name}_and_mul.cu"]
    write_if_different(
        sources[0],
        get_act_and_mul_cu_str(act_func_name, act_func_def),   # 渲染后落盘
    )
    return gen_jit_spec(f"{act_func_name}_and_mul", sources)
```

注意 activation 把文件**直接写在 `FLASHINFER_GEN_SRC_DIR` 根目录下**，而不是建一个以 URI 命名的子目录——因为它只有一个源文件，无需子目录收纳。

**norm：原地引用只读源文件。** [flashinfer/jit/norm.py:21-33](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/norm.py#L21-L33)：

```python
def gen_norm_module() -> JitSpec:
    nvcc_flags = ["-DENABLE_BF16", "-DENABLE_FP8"]
    return gen_jit_spec(
        "norm",
        [
            jit_env.FLASHINFER_CSRC_DIR / "norm.cu",                    # 只读区
            jit_env.FLASHINFER_CSRC_DIR / "flashinfer_norm_binding.cu", # 只读区
        ],
        extra_cuda_cflags=nvcc_flags,
    )
```

norm 连拷贝都省了——直接把 `CSRC_DIR` 里的 `.cu` 路径交给 `gen_jit_spec`，编译器去读只读区即可。它也不需要 Jinja（norm 不按 dtype 特化源码，而是在 kernel 内部用 dispatch 宏处理）。

**attention：渲染配置 + 拷贝主体。** 最完整的形态见 [flashinfer/jit/attention/modules.py:134-164](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L134-L164)：先 `os.makedirs(gen_directory)`（这里 `gen_directory = GEN_SRC_DIR / uri`，**按 URI 建子目录**），再渲染 `batch_mla_config.inc`，最后用一个循环把 `batch_mla_plan.cu`、`batch_mla_run.cu`、`batch_mla_binding.cu` 三个文件从 `CSRC_DIR` 逐个 `write_if_different` 拷到子目录。三种生成器对比如下：

| 生成器 | URI | 是否建 URI 子目录 | 是否用 Jinja | 源文件来源 |
|--------|-----|------------------|-------------|-----------|
| `gen_act_and_mul_module` | `silu_and_mul`（名字） | 否 | 是（渲染整份 `.cu`） | **生成** |
| `gen_norm_module` | `norm`（固定） | 否 | 否 | **引用 `CSRC_DIR`** |
| `gen_batch_mla_module` | 长描述串 | 是 | 是（渲染 `.inc`） | **拷贝 `CSRC_DIR`** |

> 一个常被忽略的细节：[flashinfer/jit/env.py:148-153](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L148-L153) 中，`FLASHINFER_GEN_SRC_DIR`（`.../generated`）与 `FLASHINFER_JIT_DIR`（`.../cached_ops`）属于可写区，而 `FLASHINFER_CSRC_DIR`（`flashinfer/data/csrc`）属于**只读区**。所以代码生成层只能往 `generated/` 写，绝不能往 `csrc/` 写——这是 u2-l2 讲过的「可写区 vs 只读区」红线在代码生成层的具体体现。

#### 4.3.4 代码实践

**实践目标**：在磁盘上找到「生成的源文件」，并验证 `write_if_different` 的幂等性。

**操作步骤**：

1. 找到本机的生成目录。先 `python -c "import flashinfer; flashinfer.show_config()"` 或读 [flashinfer/jit/env.py:148-150](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L148-L150)，确认 `FLASHINFER_GEN_SRC_DIR` 指向 `~/.cache/flashinfer/<版本>/<arch>/generated`。
2. 触发一次激活编译（需 GPU）：

   ```python
   import torch, flashinfer
   x = torch.randn(4, 32, device="cuda", dtype=torch.float16)
   flashinfer.silu_and_mul(x)   # 首次调用触发 JIT
   ```
3. 进入 `generated/` 目录，查看是否出现 `silu_and_mul.cu`，用编辑器打开它，对照 [flashinfer/jit/activation.py:25-69](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L25-L69) 的模板，确认 Jinja 占位符已被替换成 `silu`。
4. 记下 `silu_and_mul.cu` 的修改时间；再次运行步骤 2 的脚本，重新检查修改时间。

**需要观察的现象**：

- 第 3 步：生成的 `silu_and_mul.cu` 是一份完整 C++，含 `void silu_and_mul(...)` 与 `act_and_mul_kernel<c_type, silu>`，不再有 `{{ }}` 占位符。
- 第 4 步：第二次运行后文件修改时间**不变**——因为 `write_if_different` 发现内容相同就没写。

**预期结果**：`generated/silu_and_mul.cu` 存在且占位符已替换；重复运行不刷新 mtime。若本机暂无 GPU，第 1、3 步可改为直接调用 `get_act_and_mul_cu_str` 打印内容，现象一致（待本地验证磁盘 mtime 部分）。

#### 4.3.5 小练习与答案

**练习 1**：`write_if_different` 为什么要「内容相同就不写」？如果每次都无条件覆盖会怎样？

**答案**：ninja 用文件修改时间（mtime）判断是否需要重编译。如果生成器每次 import 都覆盖文件、刷新 mtime，ninja 就会误以为源码变了、每次都重新编译 `.so`，JIT 缓存形同虚设。「内容相同就不写」让 mtime 在内容稳定时保持不变，从而让磁盘缓存真正生效。

**练习 2**：norm 为什么可以不拷贝、直接把 `CSRC_DIR` 路径当 `sources`？这样做有什么前提？

**答案**：因为 norm 的源码不依赖任何编译期参数（dtype 等在 kernel 内用 dispatch 宏处理），所有用户共享同一份 `norm.cu`，没必要复制副本。前提是 `CSRC_DIR` 在编译时**可读**——它确实是只读区，读权限总是有的，所以成立。一旦某种算子需要按参数特化源码（如 attention），就必须先渲染/拷贝到可写区再编译。

---

### 4.4 gen_jit_spec：装配配料单并登记

#### 4.4.1 概念说明

前三步（算 URI、渲染、产生源文件）做完后，手上有了「名字」和「一份/几份 `.cu` 路径」。但编译还需要更多配料：编译选项（`-std=c++17`、`-use_fast_math`、各种 `-D` 宏）、是否需要 device linking 等。`gen_jit_spec` 就是把这些配料**组装成一个 `JitSpec` 数据类**，并顺手把它**登记进全局注册表** `jit_spec_registry`（u1-l4 讲过，CLI 的 `list-modules` 就读这张表）。

注意：`gen_jit_spec` **只装配、不编译**。真正的编译发生在后续调用 `.build_and_load()` 时（u2-l2 已讲）。这呼应了 u1-l4 的核心结论「登记 ≠ 编译」。

#### 4.4.2 核心流程

```
gen_jit_spec(name, sources, extra_cuda_cflags=None, ...)
        │
        ├──► check_cuda_arch()         # 校验 SM ≥ 7.5
        ├──► 组装 cflags / cuda_cflags  # 注入 -std=c++17、-O3、-DFLASHINFER_ENABLE_* 等
        ├──► JitSpec(name=..., sources=..., extra_cuda_cflags=..., ...)
        └──► jit_spec_registry.register(spec)   # 登记，供 CLI 查询
              │
              ▼
          返回 JitSpec（尚未编译）
```

#### 4.4.3 源码精读

`gen_jit_spec` 定义在 [flashinfer/jit/core.py:404-484](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L404-L484)。关键几段：

```python
def gen_jit_spec(name, sources, extra_cflags=None, extra_cuda_cflags=None,
                 extra_ldflags=None, extra_include_paths=None,
                 needs_device_linking=False) -> JitSpec:
    check_cuda_arch()                       # SM 门禁
    ...
    cuda_cflags = [
        *get_nvcc_parallelism_flags(),
        "-use_fast_math",
        "-Xfatbin=-compress-all",
        "-DFLASHINFER_ENABLE_F16",
        "-DFLASHINFER_ENABLE_BF16",
        ...
    ]
    if debug:
        cuda_cflags += ["-g", "-O0", "--device-debug", ...]   # FLASHINFER_JIT_DEBUG=1
    else:
        cuda_cflags += ["-DNDEBUG", "-O3"]
    ...
    spec = JitSpec(name=name, sources=[Path(x) for x in sources],
                   extra_cflags=cflags, extra_cuda_cflags=cuda_cflags, ...)
    jit_spec_registry.register(spec)        # 登记进全局表
    return spec
```

两个要点：

1. **统一注入公共编译选项**：所有模块共享 `-std=c++17`、`-use_fast_math`、`-O3`、一组 `-DFLASHINFER_ENABLE_*` 宏；各生成器通过 `extra_cuda_cflags` 追加自己的选项（例如 norm 追加 `-DENABLE_BF16`，attention 的 fa3 后端追加 `sm90a_nvcc_flags`）。这避免了每个 `gen_*_module` 都重复写一遍公共 flags。
2. **登记但不编译**：`is_compiled` 只检查 `.so` 是否存在（[flashinfer/jit/core.py:263-265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L263-L265)），因此刚 `register` 完的模块在 `module-status` 里显示「Not Compiled」是正常的。

最终，整个 activation 生成器的五步可以浓缩成一张图（对照 [flashinfer/jit/activation.py:105-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L105-L117)）：

| 步骤 | activation 的实现 | 对应代码 |
|------|------------------|---------|
| ① 算 URI | `f"{act_func_name}_and_mul"` | `gen_jit_spec` 第一个参数 |
| ② 建生成目录 | `FLASHINFER_GEN_SRC_DIR`（无子目录） | `os.makedirs(gen_directory, exist_ok=True)` |
| ③ 渲染 Jinja | `get_act_and_mul_cu_str(...)` | 整份 `.cu` 由模板渲染 |
| ④ 产生源文件 | `write_if_different` 落盘 | （activation 是「生成」而非「拷贝」） |
| ⑤ 返回 JitSpec | `gen_jit_spec(name, sources)` | 装配 + 登记 |

> 注意 activation 是「极简版」：它把 ②③④ 合并得很紧凑，且没有 attention 那种「按 URI 建子目录 + 渲染小 `.inc` + 拷贝多个 `.cu`」的完整动作。正因为极简，它最适合作为入门范例；要看完整的五步，回到 [flashinfer/jit/attention/modules.py:112-205](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L112-L205) 的 `gen_batch_mla_module`。

#### 4.4.4 代码实践

**实践目标**：观察 `gen_jit_spec` 注入的公共编译选项，以及「登记 ≠ 编译」。

**操作步骤**（无需 GPU）：

1. 运行下面脚本（示例代码），调一次生成器、查看 JitSpec：

   ```python
   from flashinfer.jit import gen_act_and_mul_module
   spec = gen_act_and_mul_module("silu")
   print("name      :", spec.name)
   print("sources   :", [str(s) for s in spec.sources])
   print("cuda_flags:", spec.extra_cuda_cflags[:6], "...")
   print("is_compiled:", spec.is_compiled)   # 大概率 False
   ```
2. 设置 `FLASHINFER_JIT_DEBUG=1` 后重跑，对比 `spec.extra_cuda_cflags` 是否多出 `-g`、`-O0`、`--device-debug` 等选项。
3. 运行 CLI `flashinfer module-status silu_and_mul`，确认它出现在注册表里且状态多为「Not Compiled」。

**需要观察的现象**：

- `spec.name` 为 `silu_and_mul`；`sources` 指向 `generated/silu_and_mul.cu`。
- `extra_cuda_cflags` 含 `-std=c++17`、`-use_fast_math`、`-DFLASHINFER_ENABLE_F16` 等公共项。
- `is_compiled` 为 `False`（除非之前已编译过）——证明「登记 ≠ 编译」。
- 开 `FLASHINFER_JIT_DEBUG=1` 后选项里出现调试标志。

**预期结果**：如上。若想让它变 `True`，需接着调 `spec.build_and_load()` 真正编译（需 GPU 与 nvcc），这部分属于编译加载层，是 u2-l2 与 u2-l5 的内容。

#### 4.4.5 小练习与答案

**练习 1**：为什么把「公共编译选项」集中放在 `gen_jit_spec` 里，而不是每个 `gen_*_module` 自己写？

**答案**：DRY（避免重复）+ 一致性。所有模块都需要 `-std=c++17`、`-O3`、同一组 `-D` 宏；集中放置既避免每个生成器抄一遍、又保证将来调整（例如升级到 C++20）只需改一处。生成器只需关心自己**特有**的 `extra_cuda_cflags`。

**练习 2**：调完 `gen_act_and_mul_module("silu")` 后，`spec.is_compiled` 通常是 `False`。那这份 `JitSpec` 有什么用？

**答案**：它是一张「编译配料单 + 登记凭证」。一方面它被登记进 `jit_spec_registry`，让 CLI 能列出它；另一方面它携带了名字、源文件、编译选项，后续 `.build_and_load()` 会照这张单子去写 `build.ninja`、调 ninja/nvcc 真正编译、再加载 `.so`。换句话说，`JitSpec` 把「编译什么」描述清楚，把「何时编译」推迟到真正需要时——这正是 JIT 的精髓。

---

## 5. 综合实践

把本讲三个最小模块（URI 计算、Jinja 渲染、源文件产生）串起来，完成一个完整任务：**为 activation 新增一个 `relu` 激活函数，并让它走完代码生成流程**。

### 实践目标

新增一种激活函数 `relu`，观察它如何获得自己的 URI、被渲染成 `.cu`、最终（可选）被编译成 `.so`。这个任务不需要你理解 kernel 内部，只考察「代码生成层」的接线。

### 操作步骤

**第 1 步：渲染验证（不改源码，最安全）。** 先用现有 API 渲染出 `relu` 的 `.cu` 文本，确认模板对新名字工作正常（示例代码）：

```python
from flashinfer.jit import get_act_and_mul_cu_str

relu_def_cu_str = r"""
__device__ __forceinline__ float relu(const float& val) {
  return val > 0.0f ? val : 0.0f;
}
"""
cu = get_act_and_mul_cu_str("relu", relu_def_cu_str)
print("含 relu_and_mul 符号:", "TVM_FFI_DLL_EXPORT_TYPED_FUNC(relu_and_mul" in cu)
print("含 relu 模板实参   :", "act_and_mul_kernel<c_type, relu>" in cu)
print(cu)   # 人工核对内容
```

确认输出含 `relu_and_mul` 函数名与 `act_and_mul_kernel<c_type, relu>` 模板实参。

**第 2 步：把 `relu` 注册进生成器（在你的工作副本里做本地实验性修改）。**

> 说明：这一步需要你在**自己克隆的工作副本**里临时改 `flashinfer/jit/activation.py`，纯属学习用途，不要提交。本讲义生成过程中不会替你改源码。

在 [flashinfer/jit/activation.py:77-102](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L77-L102) 处增加 `relu` 的定义并加入字典：

```python
relu_def_cu_str = r"""
__device__ __forceinline__ float relu(const float& val) {
  return val > 0.0f ? val : 0.0f;
}
"""

act_func_def_str = {
    "silu": silu_def_cu_str,
    "gelu": gelu_def_cu_str,
    "gelu_tanh": gelu_def_tanh_cu_str,
    "relu": relu_def_cu_str,          # ← 新增
}
```

**第 3 步：触发生成与（可选）编译。**

```python
from flashinfer.jit import gen_act_and_mul_module
spec = gen_act_and_mul_module("relu")
print("URI      :", spec.name)             # 预期 relu_and_mul
print("sources  :", spec.sources)          # 预期 .../generated/relu_and_mul.cu
print("compiled :", spec.is_compiled)      # 预期 False

# 可选（需 GPU + nvcc）：真正编译并加载
# mod = spec.build_and_load()
# out = torch.empty(...); mod.relu_and_mul(out, inp, True)
```

**第 4 步：核对磁盘产物。** 到 `FLASHINFER_GEN_SRC_DIR`（`~/.cache/flashinfer/<版本>/<arch>/generated`）下打开 `relu_and_mul.cu`，确认它与第 1 步打印的文本一致、且 Jinja 占位符已被替换。

### 需要观察的现象

- 第 1 步：`relu` 作为新名字被模板正确接纳，函数名与模板实参都换成 `relu`。
- 第 3 步：`spec.name == "relu_and_mul"`，源文件落在 `generated/relu_and_mul.cu`，`is_compiled` 为 `False`（仅生成、未编译）。
- 第 4 步：磁盘上的 `relu_and_mul.cu` 与渲染文本一致。

### 预期结果

- 新增 `relu` 后，它自动获得 URI `relu_and_mul`、被渲染成同名 `.cu`、写进 `generated/`，并被 `gen_jit_spec` 登记进注册表——整套流程无需你写任何 kernel 主体逻辑。
- 编译是否成功取决于 `relu` 的 C++ 定义是否正确（参考 4.2.5 练习 2 的提醒：Jinja 不检查 C++ 语义）。若 `build_and_load()` 报错，多半是定义片段本身的问题，而非代码生成层的失败。
- 若本机无 GPU/nvcc，第 1、2、3、4 步（生成与登记部分）仍可完成，仅 `build_and_load()` 这一步「待本地验证」。

## 6. 本讲小结

- `gen_*_module` 这类函数遵循**标准五步**：算 URI → 建生成目录 → 渲染 Jinja → 产生源文件（生成或拷贝）→ `gen_jit_spec` 装配并返回 `JitSpec`。
- **URI 是模块的唯一名字与缓存键**，只编码编译期参数。activation 因参数极少，URI 退化为普通名字（`silu_and_mul`）；attention 参数多，URI 是一长串可读的描述字符串。
- **Jinja 渲染**做参数特化：activation 把整份 `.cu` 写成模板、用 `{{ act_func_name }}` / `{{ act_func_def }}` 两个洞接纳不同激活；attention 则只渲染一个小 `.inc` 配置头。
- 产生源文件有两条路：activation **直接生成**并经 `write_if_different` 幂等落盘；norm **原地引用**只读的 `CSRC_DIR`；attention **拷贝** `CSRC_DIR` 的 `.cu` 到按 URI 命名的可写子目录。三者都遵守「只往 `generated/` 写、不碰只读 `csrc/`」的红线。
- `write_if_different` 的「内容相同就不写」是磁盘缓存生效的关键，避免无谓刷新 mtime 触发重编译。
- `gen_jit_spec` 统一注入公共编译选项（`-std=c++17`、`-O3`、`-DFLASHINFER_ENABLE_*`），把 `JitSpec` 登记进 `jit_spec_registry`，但**登记 ≠ 编译**——真正编译在 `.build_and_load()`。

## 7. 下一步学习建议

本讲讲清了「代码生成层」如何产出一份 `.cu` 和一张 `JitSpec`。接下来：

- **编译上下文与架构目标（u2-l4）**：`gen_jit_spec` 里出现的 `check_cuda_arch()`、`get_nvcc_parallelism_flags()` 以及各 `sm90a_nvcc_flags` 都来自编译上下文，下一讲会讲清 FlashInfer 如何决定「为哪些 SM 架构编译」。
- **模块缓存与失效（u2-l5）**：本讲多次提到 `is_compiled`「只看 `.so` 是否存在」，但「源码改了为何会自动重编译」涉及两级缓存与基于源码哈希的失效，这是 u2-l5 的主题。
- **进阶阅读**：想看最完整的五步生成器，直接读 [flashinfer/jit/attention/modules.py:112-205](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L112-L205) 的 `gen_batch_mla_module`，它集齐了「URI 子目录 + Jinja 配置 + 拷贝多源 + 架构 flags」。
- **跨单元铺垫**：等进入第 3 单元（注意力基础）后，你会反复回到本讲的五步框架——理解了 attention 的 `gen_batch_decode_module`，就理解了所有 attention wrapper 的「编译期」一半。
