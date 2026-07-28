# 基类 ncnn_llm_base 与公共能力

## 1. 本讲目标

本讲是「LLM 推理主链路」单元的第一讲，进入源码主干之前，先认识所有模态运行时共同继承的那个根基类 `ncnn_llm_base`。

读完本讲，你应当能够：

- 说清 `KVCache` 这个类型别名长什么样、为什么是「一串 (key, value) 对」，以及它在跨模态复用中的地位。
- 解释 `create_option()` 如何把「线程数 / 是否用 Vulkan」这两项用户设置翻译成 ncnn 能理解的 `ncnn::Option`，并知道它在真实派生类里被赋给 `Net::opt`。
- 看懂 `load_net()` 这层薄封装为什么是「统一加载 + 健康检查」的关键。
- 读懂基类内置的采样函数 `sample_logits()`，理解 greedy、temperature、top_k、top_p 四种策略如何串联，并意识到项目里其实存在「两套采样实现」。

本讲只看一个文件 [`src/ncnn_llm_base.h`](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h)，再用 `nllb_600m` 作为「派生类怎么用基类」的真实范例。模型如何 prefill、如何 generate，都留给后续讲义。

## 2. 前置知识

本讲假设你已完成 U1，具备以下认知（若模糊请先回看对应讲义）：

- **ncnn 是什么**：一个纯 C++ 的神经网络推理引擎，提供 `ncnn::Net`（网络容器）、`ncnn::Extractor`（一次推理的执行器）、`ncnn::Mat`（张量）、`ncnn::Option`（推理选项）。ncnn_llm 在它之上做 LLM 调度（见 u1-l1）。
- **target 与静态库**：xmake 把项目编成两个静态库——分词器库 `ncnn_tokenizer` 和核心运行时库 `ncnn_llm`（依赖前者），所有示例 / 基准 / 测试都依赖 `ncnn_llm`（见 u1-l2、u1-l3）。
- **model.json 三块**：`params` / `tokenizer` / `setting`（见 u1-l5）。本讲会用到的事实是：构造函数读取这些字段后，最终要通过 `load_net()` 把 `.param` / `.bin` 装进 `ncnn::Net`。
- **KV cache 是什么（直觉）**：Transformer 自回归生成时，每生成一个新 token 都要「回看」之前所有 token 的注意力中间结果。把这些中间结果缓存下来避免重算，就是 KV cache。本讲只关心它的**数据类型**，具体怎么填充、怎么推进交给 u2-l3 / u2-l4。

几个本讲会反复出现的 C++ 概念，先一句话解释：

| 术语 | 一句话解释 |
|---|---|
| `protected` 构造函数 | 只能被派生类调用，外部无法直接 `new`。这正是「必须继承才能用」的写法。 |
| 类型别名 `using X = Y;` | 给一个已有类型起个短名，不产生新类型。 |
| `ncnn::Mat` | ncnn 的张量，按 `w/h/c`（宽/高/通道）描述形状，`elemsize` 是每个元素的字节数。 |
| `ncnn::Option` | 控制 ncnn 推理行为的配置项，如线程数、是否用 Vulkan GPU、是否用 bf16 存储。 |
| 采样 (sampling) | 从模型输出的 logits（一组原始分数）里「挑」出一个 token id 的过程。 |

## 3. 本讲源码地图

本讲只精读一个文件，但会引用若干「消费方」来印证：

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| `src/ncnn_llm_base.h` | 定义基类 `ncnn_llm_base`、`KVCache` 类型别名、若干 `ncnn::Mat` 工具函数 | **本讲主角**，唯一精读对象 |
| `src/nllb_600m.cpp` | NLLB 翻译运行时，其 `Impl` 继承自 `ncnn_llm_base` | **真实消费方**，证明 `create_option / load_net / sample_logits / KVCache` 如何被使用 |
| `src/sampling.h` / `src/sampling.cpp` | 与基类私有的采样函数**逐字重复**的一组自由函数 | 用来点破「两套采样实现」的存在（深究留到 u3-l4） |
| `src/ncnn_text_runtime.cpp` | 共享文本运行时，其 `llm_select_next_token` 用的是 `sampling.cpp` 那一套 | 印证另一条采样路径 |

> 提醒：`ncnn_llm_base.h` 是个**纯头文件**（header-only），没有对应 `.cpp`。它只放声明 + 内联实现，被 `ncnn_llm_gpt.h`、`ncnn_llm_ocr.h`、`ncnn_llm_asr.h`、`ncnn_embedding.h`、`nllb_600m.cpp`、`ncnn_text_runtime.h` 统一 `#include`。可以说，它是全模态运行时的「公共底座头」。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. `KVCache` 类型别名 + 若干 `ncnn::Mat` 工具函数
2. `create_option()`：把「线程 / Vulkan」设置下发给 ncnn
3. `load_net()`：统一的网络加载与健康检查
4. `sample_logits()`：基类内置采样（softmax / top_k / top_p）

### 4.1 KVCache 类型与 ncnn::Mat 工具函数

#### 4.1.1 概念说明

ncnn_llm 支持六类模态（LLM / VLM / OCR / ASR / 翻译 / 嵌入），但它们在「自回归解码」这件事上惊人地一致：都需要维护一份 KV cache，每步拿上一步的 token 去更新它。既然数据结构是共享的，项目就在最底层的 `ncnn_llm_base.h` 里给它起了一个统一的类型别名 `KVCache`，让所有模态和工具函数引用同一个名字，避免各写各的。

同样出于「多个模态都要用」，这个头文件还顺手放了几个对 `ncnn::Mat` 的通用小工具：

- `mat_from_int_vector`：把一串 token id（`int`）塞进一个一维 `ncnn::Mat`，方便喂给网络输入。
- `add_mats_inplace`：逐元素相加两个形状相同的 `Mat`（用于把 token embed 与位置 embed 加在一起）。
- `argmax1d`：在一维 `Mat` 里找最大值下标——这就是「贪心解码 (greedy)」的核心。
- `sinusoidal_positional_embedding` / `sinusoidal_positional_embedding_for_pos`：生成正弦位置编码（NLLB 这类老架构在用，现代 LLM 多改用 RoPE，见 U4）。

#### 4.1.2 核心流程

`KVCache` 的结构可以用一句话概括：

```
KVCache = 一层一层的 (K, V) 对
        = std::vector< std::pair<ncnn::Mat K, ncnn::Mat V> >
```

- **外层 vector 的长度 = Transformer 的层数**。例如 NLLB-600m 有 24 层 decoder，那么 `KVCache` 就有 24 个元素（见 `nllb_600m.cpp` 里的 `kNumDecoderLayers = 24`）。
- **每一层一对 `Mat`**：该层注意力的 key 缓存与 value 缓存。
- prefill 阶段一次性算出整段的 K/V 并塞进 `KVCache`；decode 阶段每步读旧 cache、算新 cache、再覆盖回去。

为什么用 `std::pair<ncnn::Mat, ncnn::Mat>` 而不是自定义 struct？因为这里只需要「两个 Mat」的语义，`pair` 足够轻量，`first`=K、`second`=V，且 STL 容器对它有现成的移动/拷贝支持。

#### 4.1.3 源码精读

类型别名只有一行：

[src/ncnn_llm_base.h:14-14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L14) —— 定义全模态共用的 `KVCache`：一串「key Mat + value Mat」的 pair。

```cpp
using KVCache = std::vector<std::pair<ncnn::Mat, ncnn::Mat>>;
```

把 token id 序列喂给网络的工具：

[src/ncnn_llm_base.h:16-20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L16-L20) —— `mat_from_int_vector` 用 `memcpy` 把 `std::vector<int>` 直接拷进一个一维 `ncnn::Mat` 的内存。

逐元素相加（注意它有形状与 `elemsize==4`（即 float）的守卫，不满足就静默返回）：

[src/ncnn_llm_base.h:22-34](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L22-L34) —— `add_mats_inplace` 把两个同形 `Mat` 逐 float 相加。

贪心解码用的 `argmax1d`：

[src/ncnn_llm_base.h:36-47](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L36-L47) —— 在一维 `Mat` 上线性扫描找最大值下标，返回 token id。

**真实消费方**：NLLB 的 decoder 正是用这个类型组织缓存的。prefill 时按层数 `reserve`，再逐层 `emplace_back`：

[src/nllb_600m.cpp:193-223](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L193-L223) —— `decoder_prefill` 建一个空 `KVCache`，按 24 层从 extractor 里抽出每层的 `out_cache_k%d` / `out_cache_v%d` 塞进去。

decode 阶段则「读旧 cache、写新 cache」：

[src/nllb_600m.cpp:225-229](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L225-L229) —— `decoder_decode` 接收旧 `KVCache`、产出新 `KVCache`，调用方再 `std::move` 覆盖（见 `nllb_600m.cpp:139`）。

而 `mat_from_int_vector` + `add_mats_inplace` 也在同一个文件里被用来「token embed + 正弦位置 embed」：

[src/nllb_600m.cpp:163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183) —— `embedding_forward` 用 `mat_from_int_vector` 喂 id，用 `sinusoidal_positional_embedding` 取位置编码，再用 `add_mats_inplace` 把两者相加。

#### 4.1.4 代码实践

**实践目标**：亲手组装一个 `KVCache`，体会「层数 = vector 长度，每层一对 Mat」。

下面是**示例代码**（非项目原有代码），你可以新建一个文件（例如 `examples/probe_base_main.cpp`）来跑。它只依赖 `ncnn::Mat` 和 `ncnn_llm_base.h`：

```cpp
// 示例代码：体验 KVCache 与 argmax1d
#include "ncnn_llm_base.h"   // 仅用到 KVCache / argmax1d / mat_from_int_vector
#include <iostream>

int main() {
    // 模拟一个 2 层、每层 K/V 形状 (seq=4, dim=8) 的 KV cache
    KVCache kv;
    kv.reserve(2);
    for (int layer = 0; layer < 2; ++layer) {
        ncnn::Mat K(8, 4); K.fill(0.1f * layer);
        ncnn::Mat V(8, 4); V.fill(0.2f * layer);
        kv.emplace_back(K, V);
    }
    std::cout << "KVCache 层数 = " << kv.size() << "\n";          // 期望 2
    std::cout << "第 0 层 K 通道数 = " << kv[0].first.c << "\n";  // 期望 8

    // 体验 argmax1d：构造 logits，最大值在第 3 个位置
    ncnn::Mat logits = ncnn_llm_base_helpers_argmax_demo(); // 伪调用，见下方说明
    return 0;
}
```

> 上面的 `ncnn_llm_base_helpers_argmax_demo()` 是占位伪调用——`argmax1d` 是头文件里的**自由函数**（不是类成员），你可以直接 `argmax1d(some_mat)` 调用。把 `logits` 换成你自己构造的 `ncnn::Mat(5)` 并填入 `{0.1, 0.2, 0.9, 0.05, 0.3}`，`argmax1d` 应返回 `2`。

**操作步骤**：

1. 构造一个 `KVCache`，塞入 2~3 个 `(K, V)` 对，打印 `kv.size()`。
2. 用 `mat_from_int_vector({10, 20, 30})` 造一个 id Mat，打印它的 `.w`（期望 3）。
3. 构造一个一维 logits Mat，用 `argmax1d` 找出最大值下标。

**需要观察的现象**：`KVCache` 的 `size()` 恰好等于你塞入的层数；`argmax1d` 总返回最大元素的下标。

**预期结果**：层数打印与塞入数一致；argmax 返回手工设定的峰值位置。**待本地验证**（取决于你如何编译这个最小程序，见 4.2.4 的构建说明）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KVCache` 要定义在 `ncnn_llm_base.h` 而不是某个具体模态（如 `ncnn_llm_gpt.h`）里？

**参考答案**：因为 LLM、OCR、ASR、NLLB 翻译**都要用**同一个结构，把它放在最底层的公共头里，所有派生类和共享运行时（`ncnn_text_runtime`）就能引用同一个类型别名，避免重复定义和类型不一致。

**练习 2**：`add_mats_inplace` 在什么情况下会「什么都不做就返回」？

**参考答案**：当两个 `Mat` 的 `w/h/c` 形状不一致，或任一方的 `elemsize != 4`（即不是 float32）时，函数直接 return，不做相加——这是一道安全护栏，防止跨精度或跨形状的非法相加。

---

### 4.2 create_option：把线程 / Vulkan 设置下发给 ncnn

#### 4.2.1 概念说明

在 u1-l4 里我们见过命令行选项 `--threads`、`--vulkan`，它们最终落到 `Options` 结构体里。这些设置怎么变成 ncnn 的行为？中间的翻译官就是 `create_option()`。

`ncnn::Option` 是 ncnn 的「推理配置单」，影响每一次 `Extractor` 的执行：用多少线程、要不要用 GPU、要不要用 bf16 存储。ncnn_llm 把「用户语义」(`use_vulkan_` / `num_threads_`) 封装成 `ncnn::Option`，再赋给每个 `ncnn::Net` 的 `.opt` 成员——之后从该 Net 创建的所有 Extractor 都自动继承这套配置。

与 `create_option` 配套的，是构造函数 / 析构函数里对 **Vulkan 生命周期的管理**：开 Vulkan 时要在构造时 `create_gpu_instance()`，析构时 `destroy_gpu_instance()`，且全部用 `#if NCNN_VULKAN` 宏包起来——编译期没开 Vulkan 时这些调用根本不存在。

#### 4.2.2 核心流程

构造一个运行时对象时，设置流动如下：

```
用户传入 use_vulkan / num_threads
        │
        ▼
ncnn_llm_base 构造函数
   ├── 存入成员 use_vulkan_ / num_threads_
   └── 若 use_vulkan_ 且编译期开了 NCNN_VULKAN：
          ncnn::create_gpu_instance()   ← 初始化 Vulkan 全局实例
        │
        ▼
派生类构造体里调用 create_option()
   ├── opt.num_threads        = num_threads_
   ├── opt.use_bf16_storage   = false   ← 基类默认关
   └── opt.use_vulkan_compute = use_vulkan_
        │
        ▼
net.opt = opt;   ← 赋给每个 ncnn::Net
        │
        ▼
之后 net.create_extractor() 出来的执行器都带这套配置
        │
        ▼
对象析构时：若用过 Vulkan → ncnn::destroy_gpu_instance()
```

两个层面要分清（u1-l2 也强调过）：

- **编译期** `NCNN_VULKAN` 宏：决定 `create_gpu_instance` 这段代码是否存在。
- **运行期** `use_vulkan_`：决定这段代码是否真的执行。

只有「编译期开了 Vulkan」＋「运行期 `--vulkan`」才会真正用 GPU。

#### 4.2.3 源码精读

基类的受保护成员与构造函数：

[src/ncnn_llm_base.h:107-120](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L107-L120) —— 受保护成员 `use_vulkan_` / `num_threads_` / `ok_` / `rng_`；构造函数存值并在开 Vulkan 时调用 `ncnn::create_gpu_instance()`。

注意构造函数是 `protected`：**外部无法直接 `ncnn_llm_base b;`**，只能通过派生类构造。这正是本讲实践要「写一个派生类」的原因。

析构函数对称地释放：

[src/ncnn_llm_base.h:122-128](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L122-L128) —— 虚析构，在用过 Vulkan 时调用 `ncnn::destroy_gpu_instance()`；`virtual` 保证 `delete 基类指针` 时派生部分也被正确析构。

核心翻译函数 `create_option()`：

[src/ncnn_llm_base.h:130-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L130-L136) —— 新建 `ncnn::Option`，把线程数与 Vulkan 开关写进去（`use_bf16_storage` 在基类层面固定为 false）。

```cpp
ncnn::Option create_option() const {
    ncnn::Option opt;
    opt.num_threads = num_threads_;
    opt.use_bf16_storage = false;
    opt.use_vulkan_compute = use_vulkan_;
    return opt;
}
```

**真实消费方**：NLLB 的 `Impl` 正是这么接线的，把同一个 `opt` 赋给三张子网：

[src/nllb_600m.cpp:54-58](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L54-L58) —— 调 `create_option()` 得到 `opt`，再分别赋给 `embed_net_` / `encoder_net_` / `decoder_net_` 的 `.opt`。

> **一个值得注意的细节（nuance）**：并非所有派生类都走 `create_option()`。例如 `ncnn_llm_gpt` 的构造函数就**直接手写** `decoder_net->opt.use_vulkan_compute = true;`（见 [src/ncnn_llm_gpt.cpp:52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L52)），而没用基类的封装。也就是说 `create_option` 是基类提供的「便捷封装」，派生类可以选择用、也可以自己直接设 `Net::opt`。NLLB 是「规规矩矩用封装」的范例。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：写一个最小的派生类，分别以 `use_vulkan=true/false` 构造，打印内部的 `num_threads_` 与 `use_vulkan_`，并打印 `create_option()` 产出的 `ncnn::Option` 字段，验证「设置确实被下发」。

因为基类构造函数是 `protected`、`create_option` 也是 `protected`，我们必须继承才能用。下面是**示例代码**（非项目原有），建议存为 `examples/probe_base_main.cpp`：

```cpp
// 示例代码：探测 ncnn_llm_base 的设置下发
#include "ncnn_llm_base.h"
#include <iostream>

// 唯一目的：把 protected 的成员/函数暴露出来便于观察
class ProbeBase : public ncnn_llm_base {
public:
    ProbeBase(bool use_vulkan, int num_threads)
        : ncnn_llm_base(use_vulkan, num_threads) {}

    // 暴露 protected 成员
    int    threads()  const { return num_threads_; }
    bool   vulkan()   const { return use_vulkan_; }

    // 暴露 protected 函数，并打印它产出的 ncnn::Option
    void dump_option() const {
        ncnn::Option opt = create_option();
        std::cout << "  opt.num_threads        = " << opt.num_threads        << "\n";
        std::cout << "  opt.use_bf16_storage   = " << opt.use_bf16_storage   << "\n";
        std::cout << "  opt.use_vulkan_compute = " << opt.use_vulkan_compute << "\n";
    }
};

int main() {
    std::cout << "[CPU 模式] use_vulkan=false, num_threads=8\n";
    ProbeBase cpu(false, 8);
    std::cout << "  threads()=" << cpu.threads() << " vulkan()=" << cpu.vulkan() << "\n";
    cpu.dump_option();

    std::cout << "[GPU 模式] use_vulkan=true, num_threads=4\n";
    ProbeBase gpu(true, 4);
    std::cout << "  threads()=" << gpu.threads() << " vulkan()=" << gpu.vulkan() << "\n";
    gpu.dump_option();

    std::cout << "ok()=" << cpu.ok() << "\n";  // 没加载网络，ok_ 仍为 true
    return 0;
}
```

**操作步骤**：

1. 新建 `examples/probe_base_main.cpp`，粘贴上面的示例代码。
2. 为它加一个 xmake target（**示例代码**，参考项目现有 target 写法）：

   ```lua
   -- 示例代码：加在 xmake.lua 里
   target("probe_base")
       set_kind("binary")
       set_languages("c++20")
       add_files("examples/probe_base_main.cpp")
       add_deps("ncnn_llm")          -- 复用核心库（含 ncnn_llm_base.h 与 ncnn 链接）
       set_rundir("$(projectdir)")
   ```

3. 执行 `xmake build probe_base && xmake run probe_base`。

**需要观察的现象**：

- CPU 模式下 `dump_option()` 打印 `num_threads=8`、`use_vulkan_compute=0`、`use_bf16_storage=0`。
- GPU 模式下打印 `num_threads=4`、`use_vulkan_compute=1`。
- `num_threads_` / `use_vulkan_` 的值与构造时传入的一致——证明成员变量被正确保存。
- 若本机编译期未开 `NCNN_VULKAN`，GPU 模式仍能跑通（`create_gpu_instance` 不存在），但 `use_vulkan_compute` 字段会被打印为 1，说明这只是「设置项」被记录，是否真用 GPU 还要看 ncnn 是否支持。

**预期结果**：两组 `opt` 打印分别反映 `false,8` 与 `true,4` 的设置。**待本地验证**（若未安装 Vulkan，GPU 构造不会崩溃，因为相关代码受 `NCNN_VULKAN` 宏守护）。

**用一句话回答实践的提问**：`create_option()` 把成员 `num_threads_` 映射到 `opt.num_threads`、把 `use_vulkan_` 映射到 `opt.use_vulkan_compute`，再由派生类把这个 `opt` 赋给 `net.opt`；自此该 Net 创建的所有 Extractor 都继承这套线程/GPU 配置。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ncnn_llm_base` 的构造函数要声明为 `protected`？

**参考答案**：因为 `ncnn_llm_base` 是个抽象的「公共底座」，本身不构成一个可运行的模型——它只提供 KV cache 类型、采样、网络加载等公共能力。把构造函数设为 protected，强制使用者必须通过派生类（`ncnn_llm_gpt` / `nllb_600m::Impl` 等）来创建对象，保证「先有具体模态，再谈公共能力」的语义。

**练习 2**：把本机编译期的 `NCNN_VULKAN` 关掉，再用 `use_vulkan=true` 构造 `ProbeBase`，会发生什么？为什么不会编译报错？

**参考答案**：程序仍能编译并运行，`use_vulkan_compute` 会被设为 1，但 Vulkan 不会真正初始化。因为 `create_gpu_instance()` / `destroy_gpu_instance()` 的调用被 `#if NCNN_VULKAN` 包了起来——宏未定义时这两行代码在预处理阶段就被删除，根本不存在，所以不会编译报错；运行期 `use_vulkan_` 只是个普通 bool，赋值/读取永远合法。

---

### 4.3 load_net：统一的网络加载与健康检查

#### 4.3.1 概念说明

ncnn 把一个网络拆成两个文件：`.param`（网络结构，文本）与 `.bin`（权重，二进制）。加载它们要调 `ncnn::Net::load_param()` 和 `load_model()`，两者都返回 0 表示成功、非 0 表示失败。

每个模态都要加载好几张子网（LLM 有 embed/decoder/proj_out，NLLB 有 embed/encoder/decoder，VLM 还要加视觉子网）。如果每次都手写「load_param + load_model + 判返回值 + 记录失败」，代码会又长又重复。`load_net()` 就是把这层样板代码收拢成一个函数，并用一个成员 `ok_` 充当「健康标志位」：任一子网加载失败，整个对象就被标记为「不可用」，之后可以用公开的 `ok()` 一次性查询。

#### 4.3.2 核心流程

```
load_net(net, param_path, bin_path)
   ├── net.load_param(param_path)  失败？
   ├── net.load_model(bin_path)    失败？
   │        任一失败 ──► ok_ = false; return false
   └── 都成功 ──► return true

之后外部调用 obj.ok() 即可知道「所有子网是否都加载成功」
```

注意它**不抛异常**，而是用返回值 + `ok_` 标志位表达失败。这与 u1-l5 里提到的「构造函数 `catch` 异常并转成 `load model failed`」是配合关系：构造函数整体用 try/catch 包住，而单个子网加载的成败则走 `load_net` 的返回值。

#### 4.3.3 源码精读

[src/ncnn_llm_base.h:138-145](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L138-L145) —— 依次加载 `.param` 与 `.bin`，任一返回非 0 就把 `ok_` 置 false 并返回 false。

```cpp
bool load_net(ncnn::Net& net, const std::string& param_path, const std::string& bin_path) {
    if (net.load_param(param_path.c_str()) != 0 ||
        net.load_model(bin_path.c_str()) != 0) {
        ok_ = false;
        return false;
    }
    return true;
}
```

公开的健康检查接口：

[src/ncnn_llm_base.h:222-224](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L222-L224) —— `ok()` 返回 `ok_`，是外部判断「这个运行时是否可用」的唯一入口。

**真实消费方**：NLLB 连续加载三张子网，每张失败都打印一条诊断信息（但不抛异常，靠 `ok()` 兜底）：

[src/nllb_600m.cpp:60-70](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L60-L70) —— 用 `load_net` 加载 embed / encoder / decoder 三张网，失败时打印错误但继续，最终成败汇总在 `ok_` 里。

#### 4.3.4 代码实践

**实践目标**：体会「单子网失败 → `ok_` 翻成 false」的早失败机制。这是一个**源码阅读型实践**（不必真去跑坏文件）。

**操作步骤**：

1. 阅读 [src/nllb_600m.cpp:60-70](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L60-L70)，注意三处 `load_net` 调用都**没有**用早返回——即便第一张网加载失败，它仍会尝试加载后两张，只是各自打印一条 `Failed to load ...`。
2. 想象把 `decoder_bin_` 改成一个不存在的路径，推演：`load_net` 第一次失败 → `ok_=false`、返回 false → 打印 `Failed to load decoder model` → 构造函数仍走完 → 调用方后续若检查 `nllb.ok()` 会得到 false。
3. （可选，**待本地验证**）在 `ProbeBase` 里加一个 `ncnn::Net net;` 成员，调用 `load_net(net, "不存在.param", "不存在.bin")`，打印返回值与 `ok()`。

**需要观察的现象**：返回 false、`ok()` 由 true 变 false；且后续任何一次 `load_net` 失败都只会让 `ok_`「保持 false」而不会恢复 true。

**预期结果**：`load_net` 对坏路径返回 false，`ok()` 变为 false。

#### 4.3.5 小练习与答案

**练习 1**：`load_net` 失败时为什么只设标志位 `ok_=false` 而不直接 `throw`？

**参考答案**：为了让单个子网的加载失败不至于让整个构造过程崩溃；多个子网可以各自报告失败、汇总到 `ok_`，由调用方在合适时机统一用 `ok()` 决定是否继续。这是「记录错误、延后决策」的容错风格，配合构造函数外层的 try/catch 一起使用。

**练习 2**：如果连续两次调用 `load_net`，第一次成功、第二次失败，`ok_` 最终是 true 还是 false？为什么 `ok()` 不能再变回 true？

**参考答案**：最终是 false。因为 `load_net` 只在失败时把 `ok_` 设为 false，**从不在成功时把它设回 true**——一旦对象被标记为不可用，就永久不可用，避免「中间坏了一次、后面又假装好了」的不一致状态。

---

### 4.4 sample_logits：基类内置采样（softmax / top_k / top_p）

#### 4.4.1 概念说明

模型每一步输出的是一组 **logits**——词表里每个 token 的原始得分。把这组分数变成「下一个 token 的 id」就叫**采样**。最简单的做法是 **greedy（贪心）**：直接取分数最大的那个 token，对应工具就是 4.1 里的 `argmax1d`。

但贪心永远选最高分，生成会单调、容易重复。为了让输出更多样，常见策略有：

- **temperature（温度）** \(T\)：在 softmax 前把 logits 除以 \(T\)。\(T>1\) 让分布更平坦（更随机），\(T<1\) 更尖锐（更确定）。
- **top_k**：只在分数最高的 k 个 token 里采样，其余直接清零。
- **top_p（nucleus sampling）**：把 token 按概率从高到低累加，保留累计概率刚好达到 p 的最小集合，其余清零。

`sample_logits()` 就是把「是否采样 + temperature + top_k + top_p」按合理顺序串起来的入口。它由一个 `SampleConfig` 结构体驱动。

> **重要 nuance（两套采样实现）**：项目里其实有**两份**几乎逐字相同的采样代码——一份是本节讲的、`ncnn_llm_base` 的**私有成员函数**；另一份是 `src/sampling.cpp` 里的**自由函数**（`softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs`）。基类的 `sample_logits` 用的是前者；而共享文本运行时的 `llm_select_next_token`（被 LLM/OCR/ASR 的 generate 调用）用的是后者那一份，且额外支持 repetition penalty。本讲只讲基类这套；两套的对比、repetition penalty 的细节留到 **u3-l4**。

#### 4.4.2 核心流程

`sample_logits` 的决策树：

```
输入：logits (一维 Mat)，cfg (SampleConfig)
   │
   ├── cfg.do_sample == false ?  ──yes──► argmax1d(logits)   ← 贪心，直接返回
   │
   └── 否（要采样）：
          ① 把 logits 拷进 vector<float> probs
          ② softmax_vec(probs, temperature)   ← 带温度的 softmax，归一化为概率
          ③ 若 top_k > 0 ：apply_top_k(probs, top_k)   ← 只留前 k 大
          ④ 若 top_p < 1.0：apply_top_p(probs, top_p)  ← 只留累计达 p 的最小集合
          ⑤ sample_from_probs(probs)           ← 按概率随机抽一个 id
```

带温度的 softmax 数学形式（先减最大值稳定数值，再除温度，再归一化）：

\[
p_i = \frac{\exp\!\left(\frac{x_i - \max(x)}{T}\right)}
           {\sum_j \exp\!\left(\frac{x_j - \max(x)}{T}\right)}
\]

`sample_from_probs` 用 `std::discrete_distribution` 按概率分布抽样，随机源是基类成员 `rng_`（一个用 `std::random_device` 播种的 `std::mt19937`）。

`SampleConfig` 的字段含义：

| 字段 | 含义 | 默认值 |
|---|---|---|
| `temperature` | softmax 温度 | `1.0f` |
| `top_k` | 只保留前 k 大（0 表示不限制） | `0` |
| `top_p` | nucleus 累计概率阈值（1.0 表示不限制） | `1.0f` |
| `do_sample` | false=贪心；true=走完整采样链 | `false` |

#### 4.4.3 源码精读

配置结构体：

[src/ncnn_llm_base.h:99-104](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L99-L104) —— `SampleConfig` 默认值组合起来恰好是「贪心 + 不限」。

采样入口：

[src/ncnn_llm_base.h:147-169](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L147-L169) —— 先判 `do_sample`：否就直接 `argmax1d`；是则 softmax→top_k→top_p→抽样。

```cpp
int sample_logits(const ncnn::Mat& logits, const SampleConfig& cfg) {
    if (!cfg.do_sample) {
        return argmax1d(logits);
    }
    std::vector<float> probs(logits.w);
    // ... 拷贝 ...
    softmax_vec(probs, cfg.temperature);
    if (cfg.top_k > 0)   apply_top_k(probs, cfg.top_k);
    if (cfg.top_p < 1.0f) apply_top_p(probs, cfg.top_p);
    return sample_from_probs(probs);
}
```

四个私有助手（注意它们与 `src/sampling.cpp` 的自由函数**实现完全相同**，只是作用域不同）：

[src/ncnn_llm_base.h:172-180](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L172-L180) —— `softmax_vec`：减最大值、除温度、exp、归一化。

[src/ncnn_llm_base.h:182-188](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L182-L188) —— `apply_top_k`：用 `std::nth_element` 找到第 k 大作为阈值，把更小的概率清零。

[src/ncnn_llm_base.h:190-215](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L190-L215) —— `apply_top_p`：按概率降序排序并累加，累计刚达到 p 时截断，保留最小集合，其余清零。

[src/ncnn_llm_base.h:217-220](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L217-L220) —— `sample_from_probs`：用 `std::discrete_distribution` 按概率随机抽一个下标，随机源是成员 `rng_`。

随机数成员（在构造时就播好种）：

[src/ncnn_llm_base.h:111-111](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L111-L111) —— `std::mt19937 rng_{std::random_device{}()};` 为随机采样提供确定性可复现的随机引擎。

**真实消费方**：NLLB 的翻译循环正是用基类 `sample_logits` 选 token：

[src/nllb_600m.cpp:128-141](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L128-L141) —— 把 `NllbConfig` 字段填进 `SampleConfig`，每步把 decoder 的 logits 交给 `sample_logits` 得到 `last_index`。

> 对照：LLM / OCR / ASR 的 generate 用的是 `llm_select_next_token`（[src/ncnn_text_runtime.cpp:85-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L85-L114)），它调的是 `sampling.cpp` 的自由函数版本。这就是「两套采样路径」的具体体现。

#### 4.4.4 代码实践

**实践目标**：用同一组 logits 分别走「贪心」和「完整采样」，直观对比 `do_sample` 开关的作用。

下面是**示例代码**（非项目原有）。它复用 4.2 的 `ProbeBase`（因为 `sample_logits` 是 `protected`，需要派生类来暴露）：

```cpp
// 示例代码：体验 sample_logits
#include "ncnn_llm_base.h"
#include <iostream>

class ProbeBase : public ncnn_llm_base {
public:
    ProbeBase(bool uv, int nt) : ncnn_llm_base(uv, nt) {}

    int pick(const ncnn::Mat& logits, const SampleConfig& cfg) {
        return sample_logits(logits, cfg);   // 暴露 protected 采样入口
    }
};

int main() {
    // 一组手工 logits：下标 2 的分数明显高于其它
    ncnn::Mat logits(5);
    float* p = logits;
    p[0] = 0.10f; p[1] = 0.20f; p[2] = 5.00f; p[3] = 0.05f; p[4] = 0.30f;

    ProbeBase probe(false, 1);

    SampleConfig greedy;      // do_sample 默认 false → 走 argmax1d
    std::cout << "greedy = " << probe.pick(logits, greedy) << "\n";   // 期望恒为 2

    SampleConfig sample;      // 开采样
    sample.do_sample = true;
    sample.temperature = 1.0f;
    sample.top_k = 3;        // 只在前 3 大里抽
    for (int i = 0; i < 5; ++i)
        std::cout << "sample #" << i << " = " << probe.pick(logits, sample) << "\n";
    return 0;
}
```

**操作步骤**：

1. 把 `pick` 加到 4.2 的 `ProbeBase` 里（或单独建文件）。
2. `greedy` 那行应**永远**打印 2（因为下标 2 分数最高）。
3. `sample` 那几行大多打印 2，但偶尔会打印 1 或 4（因为 top_k=3 留下了前 3 大 token，给了非最大项一点概率）。

**需要观察的现象**：贪心路径输出固定；采样路径输出有随机性，但因 top_k=3 把低分项清零，绝不会抽到下标 0 或 3。

**预期结果**：greedy 恒为 2；sample 以高概率为 2、小概率为其它保留项。**待本地验证**（随机性导致每次输出可能略有不同）。

#### 4.4.5 小练习与答案

**练习 1**：如果 `do_sample=true` 但 `top_k=0` 且 `top_p=1.0`，采样行为等价于什么？

**参考答案**：等价于「在完整 softmax 分布上按概率随机抽一个 token」——不做任何截断，最多样、最不可控。此时 temperature 越大分布越平坦、输出越随机。

**练习 2**：基类的 `sample_logits` 和共享运行时的 `llm_select_next_token` 都能选 token，为什么项目要保留两份？

**参考答案**：历史与分工使然。基类 `sample_logits` 服务于像 NLLB 这样直接继承基类、自己跑解码循环的运行时；`llm_select_next_token` 服务于「共享文本运行时」（LLM/OCR/ASR 复用同一套 prefill/generate），它额外支持 repetition penalty（对历史 token 惩罚），因此用了 `sampling.cpp` 那一份。两份实现目前逐字相同，属于可被统一的技术债，对比细节见 u3-l4。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**端到端的最小探测程序**，它一次性验证 `KVCache`、`create_option`、`load_net`、`sample_logits` 四项公共能力。

**任务**：在 `examples/` 下新建 `probe_base_main.cpp`，写一个 `ProbeBase : public ncnn_llm_base`，完成下面四件事并打印结果：

1. **KVCache**：构造一个 3 层的空 `KVCache`，打印层数。
2. **create_option**：分别以 `(use_vulkan=false, num_threads=8)` 与 `(use_vulkan=true, num_threads=4)` 构造两个 `ProbeBase`，各自调用 `dump_option()` 打印 `ncnn::Option` 的三个字段。
3. **load_net**：声明一个 `ncnn::Net net;`，调用 `load_net(net, "不存在的.param", "不存在的.bin")`，打印返回值与 `ok()`，确认 `ok()` 变为 false。
4. **sample_logits**：构造一维 logits（峰值在中间），先贪心、再开采样各取若干次，对比输出。

参考实现可直接合并 4.1.4 / 4.2.4 / 4.3.4 / 4.4.4 的示例代码片段。按 4.2.4 的 xmake target 说明编译运行（`xmake build probe_base && xmake run probe_base`）。

**交付**：一段不超过 10 行的观察记录，说明：
- `ncnn::Option` 的字段如何随构造参数变化（印证 `create_option` 的下发）；
- `ok()` 在 `load_net` 失败前后的变化（印证健康检查）；
- 贪心与采样输出的差异（印证采样链）。

> 这是一个**纯 CPU 即可完成**的实践：即便本机没有 Vulkan 或模型权重，前三步都不依赖真实模型；第四步用手工 logits 即可。涉及实际运行的部分标注**待本地验证**。

## 6. 本讲小结

- `ncnn_llm_base.h` 是全模态运行时的公共底座头，定义了 `KVCache` 类型别名（一串 `(K, V)` 对，层数 = vector 长度）和一组 `ncnn::Mat` 工具函数（`mat_from_int_vector` / `add_mats_inplace` / `argmax1d` / 正弦位置编码）。
- `create_option()` 是「用户设置 → ncnn 行为」的翻译官：把 `num_threads_` / `use_vulkan_` 写进 `ncnn::Option`，由派生类（如 NLLB）赋给 `Net::opt`；Vulkan 的全局实例在构造/析构里成对管理，受 `NCNN_VULKAN` 宏守护。
- `load_net()` 把「加载 .param + .bin + 判失败」收拢为一行，并用 `ok_` 标志位 + 公开 `ok()` 提供「单次失败、永久不可用」的健康检查。
- `sample_logits()` 把 greedy / temperature / top_k / top_p 串成一条采样链，随机源是成员 `rng_`；它与 `src/sampling.cpp` 的自由函数构成「两套采样实现」，后者被共享文本运行时的 `llm_select_next_token` 使用。
- 基类构造函数是 `protected`，所以必须通过派生类使用——这正是本讲所有实践都要先写 `ProbeBase` 的根本原因。

## 7. 下一步学习建议

本讲把「公共底座」讲完了，接下来进入 U2 的主干：

- **u2-l2（ncnn 调用模式与共享文本运行时）**：看 `ncnn::Extractor` 的 `in0/out0`、`cache_k%d/cache_v%d` 命名约定，以及本讲提到的 `llm_select_next_token` 所属的共享文本运行时四个函数——本讲的 `KVCache` 在那里被真正填充和推进。
- **u2-l3（prefill 文本预填充流程）**：看 `ncnn_llm_gpt::prefill` 如何用本讲的 `load_net` 加载的子网、如何生成 RoPE cache、如何构造因果掩码并产出第一份 `KVCache`。
- **u3-l4（采样与解码策略）**：深入对比本讲点破的「两套采样实现」，并讲清 `llm_select_next_token` 的 repetition penalty。

建议阅读顺序：先 u2-l2（理解 KV cache 怎么动起来），再 u2-l3（看 prefill 如何初始化它），最后回到 u3-l4（把采样彻底讲透）。
