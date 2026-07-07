# ImplBase 派发框架与 Feature 声明

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 FlashMLA 为什么需要一个「ImplBase 派发框架」，它解决的是哪一类问题。
- 读懂 `ImplBase` 模板基类的三个核心方法（`run`、`run_`、`get_supported_features`）以及它们之间的调用关系。
- 用 `DECLARE_SUPPORTED_FEATURES` 宏为一个新实现类声明「我支持哪些 feature」。
- 解释「需求 feature 集合 ⊆ 实现 feature 集合」这个子集校验，以及在失败时如何打印一份可读的诊断信息并中止。
- 理解 `__PRETTY_FUNCTION__` 反射技巧如何把 `enum` 值变成字符串名，让错误信息对人友好。

本讲承接 [u2-l3 Arch 检测与 DISPATCH 宏](u2-l3-arch-and-dispatch-macros.md)：那里讲的是「把运行时值编译期化」的 `DISPATCH_*` 宏，本讲讲的是「在多个已编译好的实现之间做运行时选择与校验」的 `ImplBase` 框架。两者互补，共同构成 sparse 路径（sparse decode / sparse prefill）的派发机制。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

### 2.1 一个实现 = 一个「能力清单」

sparse 注意力的同一份计算（解码或 prefill）在仓库里有好几份不同的 kernel 实现：SM90 一份、SM100 上又按 head 数（64/128）、head 维度（576/512）拆成好几份。每个实现只对**某些**输入组合成立。

可以把它想象成「每个实现都自带一张能力清单」：

- 实现 A 说：我能处理 `HEAD_64`、`HEAD_DIM_576`、`HEAD_DIM_512`……
- 实现 B 说：我只能处理 `HEAD_128`、`HEAD_DIM_512`……

当一次请求到来时，接口函数先根据「这次请求需要什么」拼出一张**需求清单**，再把需求清单交给某个实现去核对：「你支持我需要的全部能力吗？」。如果支持，就执行；不支持，就报错。

这就是本讲的中心思想：**实现 = 支持的 feature 集合**，派发 = 选一个能覆盖需求集合的实现。

### 2.2 为什么要做子集校验

你可能会问：接口函数明明已经根据架构和形状选好了实现，为什么还要再校验一次？

原因有两层：

1. **防御性编程**。接口函数的选择逻辑是人写的，可能漏掉某个组合。多一道 `run()` 内部的校验，能在「选错实现」时立刻、清晰地暴露问题，而不是让 kernel 跑出错误数值或越界。
2. **支持「优先 / 兜底」选择**。在 sparse prefill 的 SM100 head128 路径里，有「小 topk 优化版」和「普通版」两个实现，接口函数需要先问它们「你支持这次需求吗？」，再决定用谁。这要求校验是一个**可单独调用**的公开方法，而不只是 `run()` 内部的一个步骤。

理解了这两点，下面的源码就会非常自然。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [csrc/api/common.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h) | 定义 `ImplBase` 模板基类、`DECLARE_SUPPORTED_FEATURES` 宏、enum 名字反射三个工具函数。本讲的核心。 |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | 定义 `DecodeFeatures` 枚举、`DecodeImplBase` 中间基类、4 个 sparse decode 实现类，以及 `sparse_attn_decode_interface` 接口函数（展示完整派发流程）。 |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | 定义 `FwdFeatures` 枚举、`FwdImplBase`、4 个 sparse prefill 实现类，以及 `sparse_attn_prefill_interface` 接口函数（展示「优先 + 兜底」选择）。 |

> 提示：`common.h` 在 [u2-l3](u2-l3-arch-and-dispatch-macros.md) 已经讲过 `Arch`、`int64_stride_to_int` 和 `DISPATCH_*` 宏，本讲只讲它**后半部分**（第 101 行之后）的 `ImplBase` 相关内容，不重复前面的内容。

## 4. 核心概念与源码讲解

### 4.1 ImplBase 基类

#### 4.1.1 概念说明

`ImplBase` 是一个**类模板**，是 sparse 路径上所有实现类的共同基类。它解决的问题是：让「多个 kernel 实现」可以用**同一套接口**被调用、被校验、被诊断。

它有两个模板参数：

- `RunArgT`：这次实现的「输入参数包」类型。对 sparse decode 是 `SparseAttnDecodeParams`，对 sparse prefill 是 `SparseAttnFwdParams`（这两个结构在 [u2-l2 统一参数结构 params.h](u2-l2-params-structs.md) 讲过）。
- `FeatureT`：用来描述能力的枚举类型。对 decode 是 `DecodeFeatures`，对 prefill 是 `FwdFeatures`。

`ImplBase` 自己不实现任何 kernel，它只定义**契约**：子类必须告诉我「你支持哪些 feature」和「你具体怎么跑」，由我来负责「先校验、再调用」。

#### 4.1.2 核心流程

`ImplBase` 对外暴露的调用顺序非常简单：

```
外部调用 impl->run(params, required_features)
        │
        ├──① check_if_all_features_are_supported_and_abort(required_features)
        │         └── 不通过 → 打印诊断 + TORCH_CHECK(false) 抛异常中止
        └──② run_(params, required_features)   ← 纯虚函数，由子类实现真正的 kernel 启动
```

这里有一个关键设计：`run_` 和 `get_supported_features` 是 **protected** 的，外部代码**不能**直接调用 `run_`；外部只能调用 public 的 `run`，而 `run` 一定会先校验。这就从访问控制层面**强制**了「先校验、再执行」。

#### 4.1.3 源码精读

先看 `ImplBase` 的整体骨架（两个类型别名 + 三个 protected 虚函数 + 三个 public 方法）：

[ImplBase 模板基类骨架 — common.h:160-173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L160-L173)

```cpp
template<typename RunArgT_, typename FeatureT_>
class ImplBase {
protected:
    using RunArgT = RunArgT_;
    using FeatureT = FeatureT_;

    virtual inline void run_(const RunArgT &params,
                             const std::vector<FeatureT> &required_features) = 0;
    constexpr virtual inline std::span<const FeatureT> get_supported_features() const = 0;
    virtual ~ImplBase() = default;
    ...
```

要点：

- `run_` 是**纯虚函数**（`= 0`），子类必须实现它，里面写真正的 kernel 启动代码。
- `get_supported_features()` 返回一个 `std::span<const FeatureT>`——这是子类「能力清单」的只读视图。它也是纯虚的，子类必须实现（通常用下一节的宏来实现，不必手写）。

再看 public 的入口方法 `run`：

[run() — 先校验再调用 run_ — common.h:226-229](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L226-L229)

```cpp
inline void run(const RunArgT &params, const std::vector<FeatureT> &required_features) {
    check_if_all_features_are_supported_and_abort(required_features);
    run_(params, required_features);
}
```

这就是「派发框架」的全部魔法：**校验通过才放行**。

> 一个容易被忽略的细节：`run_` 的第二个参数 `required_features`，在仓库现有的 8 个实现里**没有任何一个真正读取它**（可以用 `grep required_features` 在 `csrc/api/` 下验证——它只出现在签名里）。实现内部的真正分支（按 head 数、head 维度等）是通过 `DISPATCH_*` 宏读 `params` 字段完成的（见 [u2-l3](u2-l3-arch-and-dispatch-macros.md)）。所以 `required_features` 在 `run_` 里目前只是个**预留的钩子**，真正起作用的是它在 `run()` 里作为校验依据的部分。

#### 4.1.4 代码实践

**实践目标**：确认「`run_` 是 protected，外部无法绕过校验直接调用」这一访问控制设计。

**操作步骤**：

1. 打开 [csrc/api/common.h:160-230](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L160-L230)。
2. 数清楚 `protected:` 块（第 165 行）下面有哪些成员，`public:` 块（第 175 行）下面有哪些成员。
3. 在 [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) 和 [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) 里搜索 `->run(` 与 `.run(`，确认接口函数**只调用 `run`，从不直接调用 `run_`**。

**需要观察的现象**：所有对外调用都形如 `impl->run(params, features)` 或 `fwd_impl.run(params, required_features)`，没有任何一处直接 `run_`。

**预期结果**：你能得出结论——任何一次 kernel 执行都必须先经过 `check_if_all_features_are_supported_and_abort`，这是由 C++ 访问控制保证的，不依赖程序员的自觉。

#### 4.1.5 小练习与答案

**练习 1**：`ImplBase` 为什么用模板参数 `FeatureT`，而不是直接写死一个全局的 `Feature` 枚举？

**参考答案**：因为 sparse decode 和 sparse prefill 的能力维度不同——decode 关心 `EXTRA_KVCACHE`、`V32_KVCACHE_FORMAT` 等 decode 专属能力，prefill 关心 `SINK_LSE` 等。把 `FeatureT` 做成模板参数，可以让两套互不干扰的枚举各自驱动一套同构的派发框架，复用 `ImplBase` 的全部校验/诊断逻辑而不必复制代码。

**练习 2**：`get_supported_features()` 为什么返回 `std::span<const FeatureT>` 而不是 `std::vector`？

**参考答案**：子类的 feature 清单通常是编译期已知的常量数组（见下一节宏里那个 `static constexpr FeatureT features[]`）。`std::span` 是对「某段连续内存」的非拥有视图，可以零拷贝地指向那个静态数组，既避免了运行时构造 vector 的开销，也表达「只读、不拥有」的语义。

---

### 4.2 DECLARE_SUPPORTED_FEATURES 宏

#### 4.2.1 概念说明

每个实现类都要实现 `get_supported_features()`。如果手写，每个类都要重复「定义一个静态数组 + override 一个返回 span 的小函数」这套样板。`DECLARE_SUPPORTED_FEATURES(...)` 宏就是把这套样板压缩成一行声明。

它的设计目标是：**让「能力清单」声明变得显眼、集中、不容易写错**。读代码时，只要看一个类顶部的这个宏，就能立刻知道它支持哪些 feature。

#### 4.2.2 核心流程

宏展开后做了两件事：

1. 在类里定义一个 `static constexpr FeatureT features[]` 数组，内容是你传进来的那些枚举值。
2. `override` 掉 `get_supported_features()`，让它 `return features;`（隐式构造 span）。

由于宏把这两段放在 `protected:` 下，所以能力清单对子类可见、对外只通过 `get_supported_features()` 暴露。

#### 4.2.3 源码精读

宏定义本身非常短：

[DECLARE_SUPPORTED_FEATURES 宏 — common.h:143-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L143-L149)

```cpp
#define DECLARE_SUPPORTED_FEATURES(...) \
protected: \
    static constexpr FeatureT features[] = { __VA_ARGS__ }; \
    constexpr inline std::span<const FeatureT> get_supported_features() const override { \
        return features; \
    }
```

> 注意 `FeatureT` 在这里直接出现——它是 `ImplBase` 的 `protected` 类型别名（见上一节 4.1.3）。所以这个宏**只能用在 `ImplBase` 的子类内部**，否则 `FeatureT` 找不到定义。

来看一个真实用法，`Decode_Sm90_Impl`（SM90 sparse decode 的唯一实现，能力最全）：

[Decode_Sm90_Impl 的 feature 声明 — sparse_decode.h:44-56](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L44-L56)

```cpp
class Decode_Sm90_Impl : public DecodeImplBase {
    DECLARE_SUPPORTED_FEATURES(
        DecodeFeatures::HEAD_64,
        DecodeFeatures::HEAD_128,
        DecodeFeatures::HEAD_DIM_512,
        DecodeFeatures::HEAD_DIM_576,
        DecodeFeatures::V32_KVCACHE_FORMAT,
        DecodeFeatures::MODEL1_KVCACHE_FORMAT,
        DecodeFeatures::ATTN_SINK,
        DecodeFeatures::TOPK_LENGTH,
        DecodeFeatures::EXTRA_KVCACHE,
        DecodeFeatures::EXTRA_TOPK_LENGTH
    )
```

这表示 `Decode_Sm90_Impl` 支持全部 10 个 `DecodeFeatures`——它是一个「全能」实现。

再对比一个**能力受限**的实现 `Decode_Sm100_Head64_Impl`：

[Decode_Sm100_Head64_Impl 的 feature 声明 — sparse_decode.h:78-89](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L78-L89)

它声明了 `HEAD_64`、`HEAD_DIM_512`、`HEAD_DIM_576` 等，但**没有** `HEAD_128`。这正是后面校验会用到的事实。

#### 4.2.4 代码实践

**实践目标**：通过对比两个实现的能力清单，体会「实现 = 支持的 feature 集合」。

**操作步骤**：

1. 打开 [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h)。
2. 列出 4 个实现类（`Decode_Sm90_Impl`、`Decode_Sm100_Head64_Impl`、`Decode_Sm100_Head64x2_Impl`、`Decode_Sm100_Head128_Impl`）各自支持哪些 `DecodeFeatures`。
3. 重点关注 `HEAD_DIM_576` 与 `V32_KVCACHE_FORMAT`：哪个实现不支持它们？

**需要观察的现象**：`Decode_Sm100_Head128_Impl`（第 156 行）的能力清单里**没有** `HEAD_DIM_576`、也**没有** `V32_KVCACHE_FORMAT`。

**预期结果**：你会得出结论——`Decode_Sm100_Head128_Impl` 只能处理 `MODEL1_KVCACHE_FORMAT` + `HEAD_DIM_512` 的请求；任何 V3.2 风格（`d_qk=576`）的 head128 请求都不能交给它。这正好解释了为什么 SM100 上 head128 + d_qk=576 要走 `Decode_Sm100_Head64x2_Impl`（调两次 head64 kernel）这条折中路径。

#### 4.2.5 小练习与答案

**练习**：`DECLARE_SUPPORTED_FEATURES` 宏用到了 `__VA_ARGS__`，说明它是变参宏。为什么 feature 列表要做成变参，而不是让用户传一个 `{...}` 初始化列表？

**参考答案**：变参宏的写法 `DECLARE_SUPPORTED_FEATURES(A, B, C)` 比 `DECLARE_SUPPORTED_FEATURES({A, B, C})` 更干净、更像函数调用，也更容易让读者一眼数清有几项。宏内部再把 `__VA_ARGS__` 展开成 `static constexpr FeatureT features[] = { A, B, C };`，由编译器负责数组大小推导，用户无需手写元素个数。

---

### 4.3 feature 校验与 abort

#### 4.3.1 概念说明

有了「能力清单」（`get_supported_features`）和「需求清单」（`required_features`），校验就是一道**子集判定**：

设需求集合为 \( R \)，实现支持集合为 \( S \)，当且仅当

\[
R \subseteq S
\]

时这次派发才合法。

`ImplBase` 提供了三个相关方法，分工明确：

| 方法 | 作用 | 返回 / 行为 |
| --- | --- | --- |
| `check_if_all_features_are_supported` | 只判定，不中止 | `bool`：是否全部支持 |
| `check_if_all_features_are_supported_and_abort` | 判定 + 不通过则打印诊断并中止 | 无返回（不通过则抛异常） |
| `run` | 校验 + 执行 | 先 abort 校验，再 `run_` |

「只判定」的方法之所以单独存在，是因为 sparse prefill 的接口函数需要它来做「优先 / 兜底」选择（见 4.3.4）。

#### 4.3.2 核心流程

校验逻辑（朴素的双重循环）：

```
for 每个需求 r ∈ R:
    在 S 中找 r
    找不到 → 整体返回 false
全部找到 → 返回 true
```

abort 流程在返回 false 时触发：

```
1. 打印 "Required features:"            （R 中每一项：序号 + 名字）
2. 打印 "Supported features:"           （S 中每一项：序号 + 名字）
3. 打印 "Features that are required but not supported:"（差集 R \ S）
4. 打印当前 GPU 信息（型号、SM 版本、SM 数）
5. TORCH_CHECK(false, ...) 抛异常中止
```

#### 4.3.3 源码精读

先看「只判定」的版本，它是整个校验的核心：

[check_if_all_features_are_supported — common.h:176-190](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L176-L190)

```cpp
inline bool check_if_all_features_are_supported(const std::vector<FeatureT> &required_features) {
    for (const auto &required_feature : required_features) {
        bool is_supported = false;
        for (const auto &supported_feature : get_supported_features()) {
            if (required_feature == supported_feature) { is_supported = true; break; }
        }
        if (!is_supported) { return false; }
    }
    return true;
}
```

这就是 \( R \subseteq S \) 的直接翻译。复杂度是 \( O(|R| \cdot |S|) \)，但两个集合都很小（feature 通常不超过 10 个），完全无需优化。

再看「判定 + 中止」的版本。它的前半段就是调用上面的判定，关键在于**失败时打印的诊断信息**：

[check_if_all_features_are_supported_and_abort 的诊断输出 — common.h:192-223](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L192-L223)

注意这几行（节选）：

```cpp
fprintf(stderr, "Required features:\n");
for (const auto &f : required_features) {
    fprintf(stderr, "  - %3d: %s\n", static_cast<int>(f), get_dynamic_enum_name(f).c_str());
}
```

这里用到了 `get_dynamic_enum_name(f)`——把一个运行时的 enum 值转成可读字符串（详见 4.4）。没有它，错误信息只会显示一串冷冰冰的整数（`0`、`3`、`7`），调试时根本看不出是哪个 feature。诊断还会列出「**需要但实现不支持**的 feature」（差集 \( R \setminus S \)），以及当前 GPU 信息，最后用 `TORCH_CHECK(false, ...)` 抛出 PyTorch 异常。

#### 4.3.4 源码精读：完整派发流程（sparse decode）

把校验放回真实调用链里看。`sparse_attn_decode_interface` 做了三件事：构造需求清单 → 选实现 → 调 `run`。

**第一步，构造需求清单**：根据这次请求的实际属性，把对应的 feature 塞进 `std::vector<DecodeFeatures> features`：

[构造 required features — sparse_decode.h:327-360](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L327-L360)

逻辑很直白：`h_q==64` 就加 `HEAD_64`，`h_q==128` 就加 `HEAD_128`；`d_qk==576` 加 `HEAD_DIM_576`，`d_qk==512` 加 `HEAD_DIM_512`；传了 `attn_sink` 就加 `ATTN_SINK`……以此类推。

**第二步，选实现**：根据架构和形状，`new` 出一个 `DecodeImplBase*`：

[选实现 — sparse_decode.h:362-381](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L362-L381)

可以看到选择逻辑本身已经相当细致（SM100 上 h_q=128 时还会按 `d_qk` 再分流到 `Head64x2_Impl` 或 `Head128_Impl`）。

**第三步，调用 `run`**：选好实现后，统一用一行调用：

[impl->run(params, features) — sparse_decode.h:468](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L468)

```cpp
impl->run(params, features);
```

`run` 内部的 abort 校验，就是这一步的「安全网」：万一上面的选择逻辑和实现的能力清单不一致，会在这里被抓住。

#### 4.3.5 源码精读：「优先 + 兜底」选择（sparse prefill）

`sparse_attn_prefill_interface` 在 SM100 head128 路径上展示了「只判定」方法的真正用武之地。那里有两个 head128 实现：

- `Fwd_Sm100_Head128_Small_TopK_Impl`：为小 topk 优化，但**只支持 `HEAD_DIM_512`**（见 [sparse_fwd.h:86-93](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L86-L93)）。
- `Fwd_Sm100_Head128_Impl`：普通版，支持 `HEAD_DIM_512` 和 `HEAD_DIM_576`（见 [sparse_fwd.h:68-76](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L68-L76)）。

选择逻辑是：

[small_topk vs regular 的选择 — sparse_fwd.h:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L220-L234)

```cpp
bool use_small_topk_impl = false;
if (
    (topk <= 1280 && small_topk_impl.check_if_all_features_are_supported(required_features)) ||
    !regular_impl.check_if_all_features_are_supported(required_features)
) {
    use_small_topk_impl = true;
}
```

读法是：

- **优先**：topk 较小（≤1280）**并且** small_topk 版本支持这次需求 → 用 small_topk。
- **兜底**：否则，如果连普通版本都**不支持**这次需求（比如某种 small_topk 不支持、但 regular 也不支持的极端组合），也强制走 small_topk，让它的 `run()` 去打印诊断、明确失败原因，而不是静默地选一个错误的实现。

这条「兜底」分支里出现的 `!regular_impl.check_if_all_features_are_supported(...)`，正是「只判定」方法的独立价值——它让接口函数能在**真正执行之前**先比较两个实现的能力。

#### 4.3.6 代码实践

**实践目标**：手动模拟一次「需要但实现不支持」的派发，观察校验如何拒绝。

**操作步骤**：

1. 假设请求是 SM100 + `h_q=128` + `d_qk=512`（MODEL1），即需求集合 \( R = \{\text{HEAD\_128}, \text{HEAD\_DIM\_512}, \text{MODEL1\_KVCACHE\_FORMAT}\} \)。
2. 翻到 [Decode_Sm100_Head64_Impl 的 feature 清单 sparse_decode.h:79-89](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L79-L89)，把它当作被错误选中的实现，写出它的支持集合 \( S \)。
3. 代入 `check_if_all_features_are_supported` 的双重循环，判断返回值。

**需要观察的现象**：`HEAD_128` 在 \( R \) 里，但 `Decode_Sm100_Head64_Impl` 的 \( S \) 里只有 `HEAD_64`（没有 `HEAD_128`）。

**预期结果**：循环在处理 `required_feature = HEAD_128` 时，内层遍历完整个 \( S \) 都找不到匹配，`is_supported` 保持 `false`，方法返回 `false`。如果这次调用走的是 `run()`，则会进入 abort 分支，打印三段诊断并把 `HEAD_128` 列入「required but not supported」，最后抛异常。

> 说明：在真实代码里，SM100 + h_q=128 永远不会选中 `Decode_Sm100_Head64_Impl`（选择逻辑在第 363-376 行已经按 `h_q` 分流），所以这个场景是**人为构造**的，用来理解校验机制。这就是「待本地验证」的思想实验型实践——无需 GPU，纸上即可完成。

#### 4.3.7 小练习与答案

**练习 1**：`check_if_all_features_are_supported_and_abort` 在打印诊断后，为什么用 `TORCH_CHECK(false, ...)` 而不是直接 `exit(1)` 或 `std::abort()`？

**参考答案**：`TORCH_CHECK(false, ...)` 会抛出一个 Python 层能捕获的 C++ 异常（`c10::Error`），经过 pybind 转成 Python `RuntimeError`。这样 Python 调用方可以用 `try/except` 处理，PyTorch 也能正确清理资源、打印栈。而 `exit(1)` / `std::abort()` 会直接杀掉整个进程，对作为 PyTorch 扩展的库来说过于粗暴。

**练习 2**：sparse decode 的接口用 `new` 在堆上创建实现对象（`impl = new Decode_Sm100_Head64_Impl();`），并在最后 `delete impl;`（[sparse_decode.h:492](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L492)）；而 sparse prefill 的接口（[sparse_fwd.h:213-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L234)）用栈上对象。为什么 decode 要用堆？

**参考答案**：decode 的选择逻辑是「先按架构、head 数、维度层层 if-else 决定具体类型，再用同一个 `DecodeImplBase*` 指针统一后续流程」——类型在多个分支里不同，只能用基类指针接住，故需堆分配（或 `std::unique_ptr`）。prefill 的选择更简单，每个分支直接构造一个具体类型的栈对象并就地 `.run()`，不需要统一的指针，所以用栈更轻量、无需 `delete`。

---

### 4.4 enum 名字反射

#### 4.4.1 概念说明

C++ 的枚举值在运行时只是一串整数。当你 `fprintf` 一个 `DecodeFeatures` 值时，默认只能看到 `3`，看不到 `HEAD_DIM_576`。FlashMLA 用一个编译期「反射」技巧，把枚举值变回它的**名字字符串**，专门服务于 4.3 里的错误诊断。

这个技巧的依据是：在 GCC/Clang 下，模板函数里的 `__PRETTY_FUNCTION__` 这个宏会展开成**包含模板实参的完整函数签名**，而签名里会出现枚举值的**名字**。把名字从签名字符串里抠出来，就完成了 enum→string 的转换。

#### 4.4.2 核心流程

整套反射分三步：

1. **`get_static_enum_name<value>()`**：给定一个**编译期常量**枚举值，从 `__PRETTY_FUNCTION__` 里抠出它的名字（如 `"HEAD_64"`）。
2. **`get_enum_max<T>()`**：递归地从 0 开始往上试，直到某个整数已经**不是合法的具名枚举值**（签名里出现 `)` 表示这是「强制转型」而非具名值），由此得到枚举值的个数（要求枚举从 0 起连续）。
3. **`get_dynamic_enum_name<T>(value)`**：用上一步的个数，在编译期把 `[0, 个数)` 的所有名字填进一个 `std::array`，运行时按下标取出对应名字——把「运行时整数」映射回「名字」。

#### 4.4.3 源码精读

**第一步**，抠名字。注意注释里标注了这段代码的出处（改编自 ykiko.me 的一篇文章）：

[get_static_enum_name — common.h:101-121](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L101-L121)

对于 `value = DecodeFeatures::HEAD_64`，GCC 会把 `__PRETTY_FUNCTION__` 展开成形如：

```
constexpr auto get_static_enum_name() [with auto value = DecodeFeatures::HEAD_64]
```

代码先定位 `=` 之后、再切掉 `::` 之前的命名空间部分，最后留下 `"HEAD_64"`。

**第二步**，数个数。这是一段 `if constexpr` 递归：

[get_enum_max — common.h:123-130](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L123-L130)

```cpp
template<typename T, std::size_t N = 0>
static constexpr std::size_t get_enum_max(){
    constexpr T value = static_cast<T>(N);
    if constexpr (get_static_enum_name<value>().find(")") == std::string_view::npos)
        return get_enum_max<T, N + 1>();
    else
        return N;
}
```

当 `N` 是合法具名值（如 0..9 对应 `HEAD_64..EXTRA_TOPK_LENGTH`），`__PRETTY_FUNCTION__` 里出现的是名字（不含 `)`）；当 `N` 越界（如 10），编译器只能渲染成 `(DecodeFeatures)10`，里面就出现了 `)`，递归停止，返回 `N`（即合法值个数）。

> 约束：这套方法要求枚举值**从 0 起连续**。仓库里的 `DecodeFeatures`（[sparse_decode.h:14-28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L14-L28)）和 `FwdFeatures`（[sparse_fwd.h:12-22](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L12-L22)）都没有显式赋值，默认从 0 连续递增，正好满足。

**第三步**，运行时查表：

[get_dynamic_enum_name — common.h:132-141](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L132-L141)

它在编译期就把所有名字算好放进 `std::array`，运行时只是一次下标访问，几乎零开销。

把它和 4.3 的 abort 输出连起来看：诊断里每一行 `  - %3d: %s` 的 `%s` 就是 `get_dynamic_enum_name(f).c_str()` 的产物。没有这三段反射，调试时你只能看到一串整数。

#### 4.4.4 代码实践

**实践目标**：亲手「人肉展开」一次反射，理解 `__PRETTY_FUNCTION__` 是如何携带名字的。

**操作步骤**：

1. 假设枚举 `enum class DecodeFeatures : int { HEAD_64, HEAD_128, HEAD_DIM_576, ... };`。
2. 对 `get_static_enum_name<DecodeFeatures::HEAD_128>()`，写出 GCC 下 `__PRETTY_FUNCTION__` 的预期内容（按 [common.h:103-110](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/common.h#L103-L110) 的解析逻辑）。
3. 对一个越界值 `static_cast<DecodeFeatures>(100)`，写出 `__PRETTY_FUNCTION__` 的预期内容，并指出它为什么含 `)`。

**需要观察的现象**：

- 对具名值 `HEAD_128`：签名形如 `... [with auto value = DecodeFeatures::HEAD_128]`，解析后得到 `"HEAD_128"`，不含 `)`。
- 对越界值 `100`：签名形如 `... [with auto value = (DecodeFeatures)100]`，含 `)`。

**预期结果**：你能解释 `get_enum_max` 为什么用「是否含 `)`」作为「是否越界」的判据——因为越界值在签名里只能以 `(T)N` 的强制转型形式出现。

> 这是「源码阅读型实践」，无需编译运行；若想实测，可在任意 GCC 环境写一个 `printf("%s\n", __PRETTY_FUNCTION__);` 的小程序验证签名格式（「待本地验证」签名细节随编译器版本略有差异）。

#### 4.4.5 小练习与答案

**练习 1**：如果有人给 `DecodeFeatures` 加一个显式赋值的枚举项，如 `FOO = 100`，这套反射会出什么问题？

**参考答案**：`get_enum_max` 会返回「第一个越界值」的位置（约 10），导致 `get_dynamic_enum_name` 的名字数组只覆盖 `[0, 10)`。运行时若传入 `FOO`（值为 100），下标 `100` 远超数组大小，造成越界访问。所以这套反射**强依赖枚举从 0 连续**，新增枚举项不能跳号。

**练习 2**：`get_static_enum_name` 用的是 `__PRETTY_FUNCTION__`（GCC/Clang）和 `__FUNCSIG__`（MSVC）两套分支。为什么非要分编译器？

**参考答案**：`__PRETTY_FUNCTION__` / `__FUNCSIG__` 是编译器内置的**非标准**宏，不同编译器展开出的签名格式不同（分隔符、命名空间写法都不一样）。代码里 `#if __GNUC__ || __clang__` 与 `#elif _MSC_VER` 两个分支分别按各自的格式去定位 `=` / `<` / `::` 等标记，才能正确抠出名字。

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个**纸面派发实验**（无需 GPU）。

**任务**：为 sparse decode 设计一个「能力受限」的假想实现，并追踪一次会触发 abort 的派发。

**步骤**：

1. **仿写实现骨架**。模仿 [Decode_Sm100_Head64_Impl（sparse_decode.h:78-107）](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L78-L107)，写一个假想的 `MyHead64Dim512Only_Impl`，要求它**只**支持 `HEAD_64` + `HEAD_DIM_512`（外加 `MODEL1_KVCACHE_FORMAT`，因为 `d_qk=512` 对应 MODEL1）。骨架应包含：

   ```cpp
   // 示例代码（非仓库原有，仅作练习）
   class MyHead64Dim512Only_Impl : public DecodeImplBase {
       DECLARE_SUPPORTED_FEATURES(
           DecodeFeatures::HEAD_64,
           DecodeFeatures::HEAD_DIM_512,
           DecodeFeatures::MODEL1_KVCACHE_FORMAT
       )
   public:
       DecodeImplMeta get_meta(int h_q, int s_q) override {
           Arch arch = Arch();
           return { std::max(arch.num_sms / s_q, 1), 5, 64 };  // 占位，照搬 Head64
       }
   protected:
       void run_(const SparseAttnDecodeParams &params,
                 const std::vector<FeatureT> &required_features) override {
           // 占位：真实实现里应调用对应 kernel
       }
   };
   ```

2. **构造一次 head128 请求**。输入 `h_q=128`、`d_qk=512`。按 [sparse_decode.h:327-360](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L327-L360) 的逻辑，需求集合 \( R \) 至少包含 `HEAD_128`、`HEAD_DIM_512`、`MODEL1_KVCACHE_FORMAT`。

3. **模拟派发**。假设（错误地）把这次请求交给了你的 `MyHead64Dim512Only_Impl`，调用 `impl->run(params, features)`。

4. **回答**：

   - `check_if_all_features_are_supported` 返回什么？为什么？（提示：`HEAD_128` ∈ \( R \) 但 ∉ \( S \)）
   - 进入 abort 分支后，「Features that are required but not supported」一段会列出哪个 feature？它的名字字符串由 4.4 的哪个函数产生？
   - 这个练习说明了 `ImplBase` 框架作为「安全网」的什么价值？

**预期结果**：你会清楚地看到——即使选择逻辑错误地把 head128 请求交给了只支持 head64 的实现，`run()` 内部的子集校验也会在 kernel 真正启动**之前**把它拦下来，并打印一份带可读名字的诊断信息，避免静默的错误计算。这正是 `ImplBase` 框架的设计意图。

## 6. 本讲小结

- `ImplBase<RunArgT, FeatureT>` 是 sparse 路径所有实现类的模板基类，把「校验 + 执行」统一成一套契约；`run_` 和 `get_supported_features` 是 protected 纯虚函数，`run` 是 public 入口，访问控制强制「先校验、再执行」。
- `DECLARE_SUPPORTED_FEATURES(...)` 宏把「能力清单」声明压缩成一行：定义 `static constexpr` 数组并 override `get_supported_features`，让每个实现的支持集合一目了然。
- 派发的数学本质是子集判定 \( R \subseteq S \)：`check_if_all_features_are_supported` 提供「只判定」能力（供 sparse prefill 的「优先/兜底」选择用），`check_if_all_features_are_supported_and_abort` 在失败时打印「需求 / 支持 / 差集」三段诊断并抛 PyTorch 异常。
- enum 名字反射（`get_static_enum_name` / `get_enum_max` / `get_dynamic_enum_name`）利用 `__PRETTY_FUNCTION__` 携带模板实参名字的特性，把运行时整数变回可读字符串，让错误诊断对人友好；它强依赖枚举从 0 连续。
- 完整派发流程是「构造 required_features → 按架构/形状选实现 → `impl->run(params, features)`」，`run` 内部的校验是选择逻辑之外的安全网。

## 7. 下一步学习建议

- 本讲讲清了 sparse 路径的**派发框架**，但具体实现类内部 `run_` 调用的 kernel 还是个黑盒。建议接着进入 [u5 FP8 Sparse Decoding Kernel（SM90）](../) 与 [u6 Token-level Sparse Prefill Kernel](../)，看 `Decode_Sm90_Impl::run_` 和 `Fwd_Sm90_Impl::run_` 真正启动的 kernel 长什么样。
- 想了解 dense 路径为何**不用** `ImplBase`（它用直接调用而非 feature 派发），可对照阅读 [u2-l1 调用链全景](u2-l1-callchain-overview.md) 里关于 dense/sparse 两种派发风格的对比。
- 若想动手扩展（比如新增一个 head_dim），[u9-l2 扩展点](../) 会给出从 `DISPATCH` 宏、`params`、Impl features 到实例化文件的端到端改动清单，本讲的 `DECLARE_SUPPORTED_FEATURES` 正是其中一环。
