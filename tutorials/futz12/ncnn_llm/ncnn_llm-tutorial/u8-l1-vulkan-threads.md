# Vulkan GPU 推理与线程/精度配置

## 1. 本讲目标

本讲是「性能、测试、导出与二次开发」单元的第一讲，聚焦一个工程实战问题：**同一个模型，怎样选择在 CPU 上跑还是在 Vulkan GPU 上跑，以及如何配置线程数与数值精度**。

学完后你应该能够：

1. 说清 `ncnn::Option` 里 `num_threads`、`use_vulkan_compute`、`use_bf16_storage`、`use_fp16_*` 各开关的含义与作用对象。
2. 解释为什么 ncnn_llm 的主运行时 `ncnn_llm_gpt` 只让 `decoder_net` 走 Vulkan、只对它开 `bf16`，而把 `fp16` 算术全部关掉。
3. 区分「编译期 `NCNN_VULKAN` 宏」与「运行期 `use_vulkan` 标志」这两个层面的 Vulkan 控制，并理解 `create_gpu_instance` / `destroy_gpu_instance` 的成对生命周期。
4. 能用命令行参数 `--use-vulkan` / `--vulkan-device` / `--threads` 启动一次 GPU 推理，并能解读 CPU 与 GPU 的耗时差异。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **ncnn 的 Net / Extractor 调用模式**（见 u2-l2）：一个网络用 `ncnn::Net` 加载 `.param`/`.bin`，每次推理用 `create_extractor()` 取一个 `Extractor`，再 `input("in0", …)` / `extract("out0", …)`。本讲讲的是「网络加载之前」对 `Net` 本身的配置。
- **ncnn_llm 的多子网结构**（见 u1-l5、u2-l1）：一个 LLM 由 `embed_net`（查表）、`decoder_net`（Transformer 主体，最重）、`proj_out_net`（投影到词表）三张子网组成；VLM 还会多出 `vision_embed_patch` / `vision_encoder` 等视觉子网。
- **基类 `ncnn_llm_base` 的公共能力**（见 u2-l1）：它提供了 `create_option()`、`load_net()`、`sample_logits()` 等被部分运行时（NLLB、ASR）继承复用的能力。

需要补充的两个底层概念：

- **GPU 实例（gpu instance）**：Vulkan 是一套图形/计算 API，要用 GPU 必须先「初始化一个全局的 Vulkan 上下文」（选择物理设备、创建逻辑设备与命令队列）。ncnn 把这一步封装成 `ncnn::create_gpu_instance()`，整个进程通常只调用一次，程序退出前用 `ncnn::destroy_gpu_instance()` 释放。
- **存储精度 vs 算术精度**：ncnn 把「数据怎么存」和「数据怎么算」分成两类开关。`*_storage` 只改存储格式（节省显存与带宽，计算时临时转换）；`*_arithmetic` 改的是真正参与乘加运算的数据类型（直接影响速度与数值范围）。

## 3. 本讲源码地图

本讲涉及的关键文件与职责如下：

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_llm_base.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h) | 全模态运行时公共底座：定义成员 `use_vulkan_`/`num_threads_`，提供 `create_option()`，并在构造/析构里用 `NCNN_VULKAN` 宏守护 `create/destroy_gpu_instance`。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 主 LLM 运行时实现：构造函数里逐网配置 `decoder_net` / `embed_net` / `proj_out_net` 及视觉子网的 `ncnn::Option`。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 构造函数声明，暴露 `use_vulkan` / `num_threads` / `vulkan_device` 三个参数。 |
| [examples/llm_ncnn_run/options.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h) | CLI 的 `Options` 结构体，含 `use_vulkan` / `num_threads` / `vulkan_device` 字段及默认值。 |
| [examples/llm_ncnn_run/options.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp) | 解析 `--use-vulkan` / `--vulkan-device` / `--threads` 三个命令行选项。 |
| [examples/llm_ncnn_run/main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp) | 把解析出的选项传给 `ncnn_llm_gpt` 构造函数。 |
| [xmake.lua](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua) | 构建期用 `configs.vulkan=true` 声明依赖的 ncnn 要带 Vulkan 编译。 |

> 提醒：ncnn_llm 存在「两套配置路径」。继承 `ncnn_llm_base` 的运行时（NLLB、ASR）走基类 `create_option()`；而主运行时 `ncnn_llm_gpt` **并不继承** `ncnn_llm_base`，它在构造函数里直接操作每个 `Net::opt`，因此配置粒度更细。本讲会对照讲解二者。

## 4. 核心概念与源码讲解

### 4.1 ncnn::Option 与基类 create_option()

#### 4.1.1 概念说明

`ncnn::Option`（简称 `opt`）是 ncnn 给每个 `ncnn::Net` 挂的「运行参数包」。一个 `Net` 在 `load_param`/`load_model` 之前，你可以改它的 `opt` 字段，ncnn 后续每次 `create_extractor()` 都会继承这份设置。和推理最相关的几个字段：

- `num_threads`：CPU 线程数（ncnn 内部用 OpenMP/线程池并行算子）。
- `use_vulkan_compute`：是否把算子下发到 Vulkan GPU 执行。
- `use_bf16_storage` / `use_fp16_storage` / `use_fp16_packed`：存储类低精度开关。
- `use_fp16_arithmetic`：算术类低精度开关。
- `vulkan_device_index`：选第几块 Vulkan 设备（多 GPU 机器才用得到）。

基类 `ncnn_llm_base` 把这些设置收拢成一个工厂函数 `create_option()`，让继承它的运行时拿到一份**统一默认**的 `Option`：线程数由成员决定、Vulkan 开关由成员决定、bf16 一律关。

#### 4.1.2 核心流程

`create_option()` 的逻辑非常直白，就是把三个成员变量翻译成 `ncnn::Option` 字段：

```text
create_option():
    opt.num_threads        ← num_threads_      (默认 4)
    opt.use_bf16_storage   ← false             (写死)
    opt.use_vulkan_compute ← use_vulkan_       (默认 false)
    return opt
```

注意三个细节：

1. `num_threads_` 默认是 **4**（基类成员默认值），但派生类构造时可以传别的值。
2. `use_bf16_storage` 在基类里被**写死为 false**——基类只给「保守的 CPU 友好默认」，更激进的 bf16 策略由各运行时自行覆盖（见 4.3）。
3. 这里**没有**设置 `vulkan_device_index`、也没有碰任何 `use_fp16_*`，意味着走基类默认的运行时使用 ncnn 自带的 fp16 默认值（通常也是关闭/保守）。

#### 4.1.3 源码精读

基类成员与 `create_option()` 定义在 [src/ncnn_llm_base.h:107-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L107-L136)：

```cpp
protected:
    bool use_vulkan_ = false;
    int num_threads_ = 4;
    bool ok_ = true;
    std::mt19937 rng_{std::random_device{}()};
    ...
    ncnn::Option create_option() const {
        ncnn::Option opt;
        opt.num_threads = num_threads_;
        opt.use_bf16_storage = false;
        opt.use_vulkan_compute = use_vulkan_;
        return opt;
    }
```

这段代码做了三件事：声明 `use_vulkan_`/`num_threads_` 两个运行期开关（带默认值），并用 `create_option()` 把它们下发到 `ncnn::Option`。基类构造函数是 `protected`（[src/ncnn_llm_base.h:113-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L113-L114)），所以 `create_option()` 实际只服务于 NLLB、ASR 这类继承它的运行时；主运行时 `ncnn_llm_gpt` 走的是 4.2 介绍的另一条路。

#### 4.1.4 代码实践

**实践目标**：理解 `create_option()` 如何把成员变量映射成 `ncnn::Option`，并确认 `num_threads` 的默认值。

**操作步骤（源码阅读型）**：

1. 打开 [src/ncnn_llm_base.h:130-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L130-L136)，逐行对照「成员变量 → opt 字段」的映射。
2. 在仓库里搜索 `create_option()` 的调用点（用 `Grep` 搜 `create_option`），看哪些运行时真的用了它（预期是 NLLB、ASR 等继承 `ncnn_llm_base` 的类）。
3. 注意 `ncnn_llm_gpt` 的构造函数（4.2）里**没有** `create_option` 的调用——确认主运行时绕过了它。

**需要观察的现象**：`create_option()` 只设置了 3 个字段，`vulkan_device_index` 和所有 `use_fp16_*` 都没动，说明基类默认是最保守的配置。

**预期结果**：能口头复述「`num_threads_`(默认4) / `use_vulkan_`(默认false) / `use_bf16_storage`(写死false)」三者如何进入 `ncnn::Option`。

#### 4.1.5 小练习与答案

**练习 1**：如果想让某个继承 `ncnn_llm_base` 的运行时跑在 GPU 上，需要改哪些地方？

**答案**：构造该运行时对象时把 `use_vulkan` 传 `true`（让基类成员 `use_vulkan_=true`），`create_option()` 就会把 `opt.use_vulkan_compute` 置为 `true`；同时还要保证 ncnn 是带 Vulkan 编译的（见 4.4）。

**练习 2**：为什么基类把 `use_bf16_storage` 写死成 `false`，而不是跟着 `use_vulkan_` 一起开？

**答案**：bf16 存储会带来微小精度损失，并非所有模型/算子都安全。基类面向所有继承者，应给最保守的默认；需要 bf16 加速的运行时（如 GPU 上的 decoder）在自己的构造函数里单独打开，避免「一开全开」误伤对精度敏感的运行时。

---

### 4.2 构造函数里的逐网配置：decoder 上 GPU，embed/proj_out 留 CPU

#### 4.2.1 概念说明

主运行时 `ncnn_llm_gpt` **不继承** `ncnn_llm_base`，因此不用 `create_option()`。它在构造函数里直接对三张子网的 `Net::opt` 逐个赋值，这样可以做到「不同子网用不同策略」。

核心设计取舍是：**只让最重的 `decoder_net` 走 Vulkan GPU，轻量的 `embed_net` 和 `proj_out_net` 留在 CPU**。原因：

- `decoder_net` 是 Transformer 主体（多层 attention + FFN），占了 99% 以上的计算量，GPU 能显著加速。
- `embed_net` 只是一次 token id 查表，`proj_out_net` 只是一次线性投影，计算极轻。把它们也送上 GPU，反而要付出「CPU↔GPU 数据拷贝 + kernel 启动」的固定开销，得不偿失。
- 视觉子网 `vision_embed_patch` / `vision_encoder`（仅 VLM 有）计算量也大，所以也送上 GPU。

#### 4.2.2 核心流程

`ncnn_llm_gpt` 构造函数对选项的配置顺序如下（伪代码）：

```text
构造 decoder_net / embed_net / proj_out_net 三个 Net

if num_threads > 0:
    三个 Net 的 opt.num_threads 都设为 num_threads     # CPU 线程数（影响 embed/proj_out 与 GPU 之外的算子）

if use_vulkan:
    if vulkan_device >= 0:
        decoder_net.opt.vulkan_device_index = vulkan_device   # 选设备，必须在开 vulkan 之前
    decoder_net.opt.use_bf16_storage   = true                 # 仅 decoder 开 bf16 存储
    decoder_net.opt.use_fp16_arithmetic = false               # fp16 算术一律关
    decoder_net.opt.use_fp16_storage   = false
    decoder_net.opt.use_fp16_packed    = false
    decoder_net.opt.use_vulkan_compute = true                 # 只让 decoder 走 GPU
# 注意：embed_net / proj_out_net 完全不碰 vulkan/bf16，保持 CPU + fp32
```

一个关键时序约束（源码注释也强调了）：**`vulkan_device_index` 必须在 `use_vulkan_compute = true` 之前设置**，否则 ncnn 可能在选好默认设备后又重新初始化。

#### 4.2.3 源码精读

构造函数签名与三个参数见 [src/ncnn_llm_gpt.cpp:18-19](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L18-L19)，声明在 [src/ncnn_llm_gpt.h:148](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L148)：

```cpp
ncnn_llm_gpt::ncnn_llm_gpt(const std::string& model_path,
                           bool use_vulkan = false,
                           int num_threads = 0,
                           int vulkan_device = 0)
```

线程数下发到三个子网（[src/ncnn_llm_gpt.cpp:32-37](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L32-L37)）：

```cpp
// Set number of threads (0 = use ncnn default which is get_cpu_count())
if (num_threads > 0) {
    decoder_net->opt.num_threads = num_threads;
    embed_net->opt.num_threads = num_threads;
    proj_out_net->opt.num_threads = num_threads;
}
```

注意 `num_threads = 0` 是「用 ncnn 默认（即 CPU 核数）」的约定，所以传 0 不写覆盖、让 ncnn 自己决定。

Vulkan 与精度配置，只作用于 `decoder_net`（[src/ncnn_llm_gpt.cpp:39-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L39-L55)）：

```cpp
if (use_vulkan) {
    printf("[ncnn_llm_gpt] Vulkan enabled, using device %d\n", vulkan_device >= 0 ? vulkan_device : 0);
    // Set specific Vulkan device BEFORE enabling vulkan compute
    if (vulkan_device >= 0) {
        decoder_net->opt.vulkan_device_index = vulkan_device;
    }
    decoder_net->opt.use_bf16_storage = true;
    decoder_net->opt.use_fp16_arithmetic = false;
    decoder_net->opt.use_fp16_storage = false;
    decoder_net->opt.use_fp16_packed = false;
    decoder_net->opt.use_vulkan_compute = true;
} else {
    printf("[ncnn_llm_gpt] Vulkan disabled, using CPU only\n");
}
```

这段代码是本讲的核心：`embed_net` / `proj_out_net` 没有出现在这个 `if` 里，所以它们永远跑在 CPU、用默认精度。

视觉子网在 VLM 分支里也被送上 GPU（[src/ncnn_llm_gpt.cpp:200-203](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L200-L203)）：

```cpp
if (use_vulkan) {
    vision_embed_patch->opt.use_vulkan_compute = true;
    vision_encoder->opt.use_vulkan_compute = true;
}
```

（可选的 `vision_embed_pos` 子网同理，见 [src/ncnn_llm_gpt.cpp:216-218](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L216-L218)。）注意视觉子网只开了 `use_vulkan_compute`，**没有**显式开 bf16——这与 decoder 的激进策略不同。

#### 4.2.4 代码实践

**实践目标**：在源码里验证「只有 decoder 走 GPU」这一设计，并搞清三个子网各自的配置来源。

**操作步骤（源码阅读型）**：

1. 在 [src/ncnn_llm_gpt.cpp:32-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L32-L55) 范围内，用三种颜色分别标注：哪些行作用于 `decoder_net`、哪些作用于 `embed_net`、哪些作用于 `proj_out_net`。
2. 数一下 `use_vulkan_compute = true` 出现在哪些 `Net` 上（预期：decoder、vision_embed_patch、vision_encoder、vision_embed_pos）。
3. 对照 CLI 选项 [options.cpp:56-69](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L56-L69)，确认 `--use-vulkan` / `--vulkan-device` / `--threads` 三个开关最终分别落到构造函数的哪个参数。

**需要观察的现象**：`embed_net->opt` 和 `proj_out_net->opt` 全程没出现 `use_vulkan_compute`。

**预期结果**：能列出一张表——decoder（GPU+bf16）、embed（CPU）、proj_out（CPU）、vision_*（GPU 无 bf16）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vulkan_device_index` 要写在 `use_vulkan_compute = true` 之前？

**答案**：ncnn 在第一次真正用到 Vulkan 计算（即 `use_vulkan_compute` 生效后创建 extractor）时会根据 `vulkan_device_index` 选择物理设备并初始化逻辑设备/队列。若先开了 compute 再改 device index，可能已经按默认设备（0 号）初始化，导致设置不生效或重复初始化。

**练习 2**：把 `embed_net` 也送上 GPU 是好主意吗？

**答案**：通常不是。`embed_net` 是一次查表，计算量极小，送上 GPU 后「host→device 拷贝 + kernel 启动」的固定开销可能比计算本身还大，反而变慢。让轻量算子留在 CPU 是 ncnn_llm 经过权衡的选择。

---

### 4.3 bf16 storage 与 fp16 开关的取舍

#### 4.3.1 概念说明

这是本讲最需要原理支撑的一节。先回顾三种 16 位浮点格式（相对 fp32 的 1+8+23 位）：

| 类型 | 符号 | 指数 | 尾数 | 动态范围（数量级） | 特点 |
| --- | --- | --- | --- | --- | --- |
| fp32 | 1 | 8 | 23 | ~±3.4e38 | 基准，范围大、精度高 |
| bf16 | 1 | 8 | 7 | ~±3.4e38 | **与 fp32 同指数位**，范围一样大，尾数少 |
| fp16 | 1 | 5 | 10 | ~±65504 | 指数位少，**最大只能到 65504**，易溢出 |

关键差异：bf16 牺牲精度换带宽，但**保留了 fp32 的动态范围**；fp16 牺牲范围换精度，最大值只有约 6.5 万。

Transformer 推理里，注意力分数 \(QK^T/\sqrt{d}\) 在长上下文或大 logit 时很容易超过 65504，一旦用 fp16 做算术就会变成 `inf`，最终污染成 `nan`。所以 LLM 推理普遍的工程结论是：

- **存储可以用低精度**（bf16 优先）：权重和 KV cache 存成 bf16，省一半显存/带宽；计算时临时转回 fp32。LLM 推理是**显存带宽受限**的，省带宽 = 提速。
- **算术要保守**（关 fp16）：避免 fp16 的溢出风险。

ncnn_llm 的选择正符合这个结论：对 decoder 开 `use_bf16_storage = true`，同时把 `use_fp16_arithmetic` / `use_fp16_storage` / `use_fp16_packed` 全部显式置 `false`。

#### 4.3.2 核心流程

decoder 在 Vulkan 开启时的精度组合：

```text
use_bf16_storage    = true    # 存储 bf16：省带宽，范围安全
use_fp16_storage    = false   # 不用 fp16 存储：避免范围问题
use_fp16_packed     = false   # 不用 fp16 打包：同上
use_fp16_arithmetic = false   # 不用 fp16 算术：避免溢出
```

用数学语言说，存储类开关控制的是「张量在显存里以哪种位宽摆放」。设一个张量在 fp32 下的值为 \(x\)，存成 bf16 后读出时得到 \(\tilde{x}\)，误差来自尾数截断：

\[
|\tilde{x} - x| \le |x| \cdot 2^{-(7+1)} = |x| \cdot 2^{-8}
\]

即相对误差约 \(2^{-8}\approx 0.4\%\)，对推理的最终 logits 通常可接受；而它的指数位没变，所以不会因为 \(x\) 很大而溢出。这正是「存储降精度、范围不降」的依据。

> 补充：u6-l4 提到 ASR 运行时为了数值精度，构造时**关闭所有** fp16/bf16，走纯 fp32。这说明 bf16 不是无条件最优——对数值极敏感的场景（音频前端、小数值范围）仍应回到 fp32。decoder 之所以敢开 bf16，是因为它的张量量级与精度容忍度经过验证。

#### 4.3.3 源码精读

精度开关全部集中在 [src/ncnn_llm_gpt.cpp:47-52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L47-L52)：

```cpp
decoder_net->opt.use_bf16_storage = true;
// decoder_net->opt.use_bf16_packed = true;   // 被注释掉了
decoder_net->opt.use_fp16_arithmetic = false;
decoder_net->opt.use_fp16_storage = false;
decoder_net->opt.use_fp16_packed = false;
decoder_net->opt.use_vulkan_compute = true;
```

两个细节值得注意：

1. 第 48 行有一条被注释掉的 `use_bf16_packed = true`——说明作者试过 bf16 打包又关掉了，留下注释作为「曾经尝试」的痕迹。
2. `use_fp16_arithmetic = false` 被显式写出，而不是依赖默认值。这是**防御性写法**：不同 ncnn 版本的 fp16 默认值可能不同，显式写死能保证行为可复现。

对照基类 [src/ncnn_llm_base.h:133](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L133) 的 `opt.use_bf16_storage = false`，可以看到主运行时**覆盖**了基类的保守默认，单独为 decoder 激进地打开了 bf16——这正是「主运行时不用 `create_option()`、自己掌控精度」的好处。

#### 4.3.4 代码实践

**实践目标**：通过对比实验，感受 bf16 存储对推理结果与速度的影响（有 GPU 时为实测，无 GPU 时为阅读分析）。

**操作步骤**：

1. **有 Vulkan GPU 的环境**：分别用以下两种方式跑同一 prompt（例如 `xmake run llm_ncnn_run assets/qwen3_0.6b`）：
   - `--use-vulkan`（decoder 开 bf16）
   - 不带 `--use-vulkan`（纯 CPU，fp32）
   对比输出文本是否仍正确、以及 tokens/s（见 u8-l2 的 benchmark 计时方式）。
2. **无 GPU 的环境（源码阅读型）**：阅读 [src/ncnn_llm_gpt.cpp:47-52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L47-L52)，解释「为何把 fp16 三连关、却把 bf16 开」。

**需要观察的现象**：bf16 开启后，生成质量通常几乎不变（相对误差 ~0.4%），但显存占用与带宽需求下降，长序列 prefill 提速明显。

**预期结果**：GPU+bf16 应明显快于纯 CPU；若发现输出出现乱码或重复，可能是该模型对 bf16 敏感，应回退 fp32（即不开 `--use-vulkan`，或本地改代码关掉 bf16 验证）。

> **待本地验证**：具体加速比与质量影响依赖你的 GPU 型号、驱动与模型，本讲无法给出确定数值，请在自己机器上实测记录。

#### 4.3.5 小练习与答案

**练习 1**：fp16 的最大值约 65504，bf16 的最大值约 \(3.4\times10^{38}\)。为什么这个差异让 LLM 推理倾向用 bf16 而不是 fp16 做存储？

**答案**：Transformer 中注意力分数、logit 等中间量量级可能很大，fp16 上限 65504 容易被突破导致溢出为 `inf`；bf16 与 fp32 共享 8 位指数，范围相同，存储时不会因为量级大而溢出，只是尾数精度略低（可接受）。

**练习 2**：`use_bf16_storage` 与 `use_fp16_arithmetic` 有什么本质区别？

**答案**：前者是**存储类**开关——张量在显存里按 bf16 摆放以省带宽，参与计算前会转回 fp32，算的是 fp32；后者是**算术类**开关——直接用 fp16 做乘加，速度快但数值范围小、易溢出。ncnn_llm 开前者、关后者，是「省带宽但保数值安全」的组合。

---

### 4.4 NCNN_VULKAN 宏守护与 gpu instance 生命周期

#### 4.4.1 概念说明

Vulkan 的启用在 ncnn_llm 里其实是**两层控制**，初学者很容易混淆：

1. **编译期**：`NCNN_VULKAN` 宏。只有当 ncnn 本身是「带 Vulkan 支持编译」的时候，这个宏才被定义，相关代码（`create_gpu_instance` 等）才会被编译进二进制。这一层由 xmake 在声明依赖时用 `configs.vulkan=true` 控制。
2. **运行期**：`use_vulkan` 布尔标志。即便二进制里编进了 Vulkan 代码，你也可以在运行时选择「这次推理不用 GPU」，从而跳过 `create_gpu_instance`、让所有算子走 CPU。

二者是「能不能用」与「用不用」的关系：编译期决定能力是否存在，运行期决定本次是否启用。

此外，Vulkan 的全局上下文（gpu instance）有严格的**成对生命周期**：进程里 `create_gpu_instance()` 调用一次，程序结束前必须 `destroy_gpu_instance()` 一次，否则会泄漏 Vulkan 资源（设备、队列、命令池）。`ncnn_llm_base` 把这对调用放在构造/析构里，并用 `NCNN_VULKAN` 宏包起来。

#### 4.4.2 核心流程

基类的生命周期管理逻辑：

```text
构造 ncnn_llm_base(use_vulkan, num_threads):
    记录 use_vulkan_ / num_threads_
    #if NCNN_VULKAN              ← 编译期能力检查
        if use_vulkan_:           ← 运行期启用检查
            ncnn::create_gpu_instance()
    #endif

析构 ~ncnn_llm_base():
    #if NCNN_VULKAN
        if use_vulkan_:
            ncnn::destroy_gpu_instance()
    #endif
```

这是一个典型的「双重门禁」：外层 `#if NCNN_VULKAN` 是编译期门禁（没编 Vulkan 时整段代码不存在），内层 `if use_vulkan_` 是运行期门禁（编了但本次不用就不调）。

> 重要：`ncnn_llm_gpt` 主运行时**不继承** `ncnn_llm_base`，所以它的构造函数里**没有**这对 `create/destroy_gpu_instance`。这意味着如果你只用 `ncnn_llm_gpt`，gpu instance 的创建/销毁由别处负责（通常是 ncnn 在第一次 `use_vulkan_compute` 的 extractor 创建时按需初始化，或由 ncnn 全局生命周期管理）。本节讲的是基类这一「规范写法」，供继承者（NLLB、ASR）参考。

#### 4.4.3 源码精读

构建期声明 ncnn 带 Vulkan，见 [xmake.lua:50-54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54)：

```lua
add_requires("ncnn master", {
    configs = {
        vulkan=true
    }
})
```

这一行让 xmake 在拉取/编译 ncnn 时打开 Vulkan，从而定义 `NCNN_VULKAN` 宏。这是「能不能用 GPU」的总开关。

基类构造/析构的宏守护见 [src/ncnn_llm_base.h:113-128](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L113-L128)：

```cpp
ncnn_llm_base(bool use_vulkan = false, int num_threads = 4)
    : use_vulkan_(use_vulkan), num_threads_(num_threads) {
#if NCNN_VULKAN
    if (use_vulkan_) {
        ncnn::create_gpu_instance();
    }
#endif
}

virtual ~ncnn_llm_base() {
#if NCNN_VULKAN
    if (use_vulkan_) {
        ncnn::destroy_gpu_instance();
    }
#endif
}
```

注意：

1. 构造里 `create` 与析构里 `destroy` 严格成对，且都受同一个 `if (use_vulkan_)` 守护，不会出现「创建了不销毁」或「没创建却销毁」。
2. 整段被 `#if NCNN_VULKAN ... #endif` 包裹，若 ncnn 没带 Vulkan 编译，这些调用根本不存在，避免链接错误。

运行期的 CLI 入口：`--use-vulkan` 在 [options.cpp:56-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L56-L63) 被解析成 `Options::use_vulkan` / `vulkan_device`，默认值见 [options.h:5-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h#L5-L12)（`use_vulkan=false`、`vulkan_device=0`、`num_threads=0`），最终在 [main.cpp:43](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L43) 传给构造函数：

```cpp
ncnn_llm_gpt model(opt.model_path, opt.use_vulkan, opt.num_threads, opt.vulkan_device);
```

#### 4.4.4 代码实践

**实践目标**：理清「编译期 vs 运行期」两层 Vulkan 控制，并能从命令行一路追到 gpu instance。

**操作步骤（源码阅读型）**：

1. 在 [xmake.lua:50-54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54) 确认 `configs.vulkan=true`，理解它决定了 `NCNN_VULKAN` 宏是否存在。
2. 沿调用链走一遍：`--use-vulkan`（[options.cpp:56-57](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.cpp#L56-L57)）→ `Options::use_vulkan`（[options.h:8](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/options.h#L8)）→ 构造函数实参（[main.cpp:43](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/llm_ncnn_run/main.cpp#L43)）→ `decoder_net->opt.use_vulkan_compute`（[ncnn_llm_gpt.cpp:52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L52)）。
3. 思考：如果不重新编译、只是运行时不加 `--use-vulkan`，会发生什么？答：gpu instance 不创建、`use_vulkan_compute` 全为 false，纯 CPU 推理。

**需要观察的现象**：运行 `xmake run llm_ncnn_run <model>` 时，控制台会打印 `[ncnn_llm_gpt] Vulkan disabled, using CPU only`（[ncnn_llm_gpt.cpp:54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L54)）；加 `--use-vulkan` 后会打印 `Vulkan enabled, using device 0`（[ncnn_llm_gpt.cpp:40](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L40)）。

**预期结果**：能画一张「命令行参数 → Options → 构造函数 → Net::opt」的传递图，并指出哪一步受编译期宏守护。

#### 4.4.5 小练习与答案

**练习 1**：如果有人把 xmake.lua 里的 `vulkan=true` 改成 `vulkan=false`，运行时再加 `--use-vulkan` 会怎样？

**答案**：`NCNN_VULKAN` 宏不再定义，基类里的 `create_gpu_instance` 代码段不会被编译；decoder 即便设了 `use_vulkan_compute=true`，ncnn 也会因为没编 Vulkan 后端而无法真正用 GPU（通常退回 CPU 或报错）。这正是「编译期能力」先于「运行期启用」。

**练习 2**：为什么 `create_gpu_instance` 与 `destroy_gpu_instance` 必须严格成对？

**答案**：Vulkan 的全局上下文持有物理设备、逻辑设备、命令队列等系统资源，泄漏会导致显存与设备句柄无法回收，长期运行会耗尽资源。成对调用保证「构造时申请、析构时释放」，是 RAII 思想的体现。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「CPU vs Vulkan」的完整对比实验。

**任务**：用同一个模型、同一段 prompt，分别以纯 CPU 和 Vulkan GPU 两种模式推理，记录并解释差异。

**步骤**：

1. **准备**：确保已按 u1-l2 用 `xmake build llm_ncnn_run` 构建出可执行文件，并在 `assets/` 下放好一个文本模型目录（如 `qwen3_0.6b`）。
2. **纯 CPU 跑一次**：

   ```bash
   xmake run llm_ncnn_run assets/qwen3_0.6b
   ```

   观察启动日志应出现 `[ncnn_llm_gpt] Vulkan disabled, using CPU only`。记录从输入 prompt 到首个 token、到生成结束的耗时（可用系统 `time` 命令粗测，精确计时见 u8-l2 的 `benchllm`）。
3. **Vulkan GPU 跑一次**：

   ```bash
   xmake run llm_ncnn_run assets/qwen3_0.6b --use-vulkan --vulkan-device 0
   ```

   观察启动日志应出现 `[ncnn_llm_gpt] Vulkan enabled, using device 0`。记录同样的耗时。
4. **线程数对比（可选）**：在 CPU 模式下加 `--threads 1` 与 `--threads 8`，观察线程数对 CPU 推理速度的影响。
5. **分析**：对照本讲 4.2 的「只 decoder 走 GPU」、4.3 的「bf16 存储省带宽」、4.4 的「编译期+运行期双层控制」，写一段说明：
   - 为什么 GPU 模式可能更快（decoder 是瓶颈、bf16 省带宽）。
   - 为什么 embed/proj_out 仍在 CPU（太轻量，搬数据不划算）。
   - 如果 GPU 模式反而更慢，可能的原因（显存拷贝开销、驱动未就绪、设备选择错误、模型层太薄导致 GPU 没占满）。

**预期结果**：得到一张含「模式 / 首 token 延迟 / 总耗时 / 输出是否正确」的对比表，并能用本讲原理解释每一项。

> **待本地验证**：以上耗时数据依赖具体硬件，本讲不预设数值。若你当前环境没有 Vulkan GPU，请把第 2～4 步作为「待本地验证」记录，转而完成 4.1～4.4 的源码阅读型实践，确保理解配置链路。

## 6. 本讲小结

- **两层 Vulkan 控制**：编译期由 [xmake.lua:50-54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54) 的 `configs.vulkan=true` 决定 `NCNN_VULKAN` 宏（能力），运行期由 `--use-vulkan` 决定本次是否启用（行为），二者是「能不能」与「用不用」。
- **基类 `create_option()`**（[ncnn_llm_base.h:130-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L130-L136)）把 `num_threads_`/`use_vulkan_` 下发到 `ncnn::Option`，但只给最保守默认（bf16 写死 false），服务 NLLB/ASR 等继承者。
- **主运行时不用基类**：`ncnn_llm_gpt` 不继承 `ncnn_llm_base`，在构造函数里逐网配置，只让 `decoder_net` 走 Vulkan（[ncnn_llm_gpt.cpp:39-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L39-L55)），轻量的 embed/proj_out 留 CPU。
- **精度取舍**：decoder 开 `use_bf16_storage=true` 省带宽（范围与 fp32 相同、安全），同时显式关掉所有 `use_fp16_*`（fp16 上限 65504 易溢出），体现「存储降精度、算术保安全」的 LLM 工程经验。
- **gpu instance 生命周期**：基类用 `#if NCNN_VULKAN` 守护 `create_gpu_instance`/`destroy_gpu_instance`，构造申请、析构释放，严格成对。
- **CLI 全链路**：`--use-vulkan`/`--vulkan-device`/`--threads` → `Options` → 构造函数 → 各 `Net::opt`，是一 条清晰可追踪的配置链。

## 7. 下一步学习建议

- **u8-l2 benchmark 性能测试**：本讲的耗时对比是手测，下一讲用 `benchllm` 做系统化测时，理解 prefill 与 generate 两阶段的 tokens/s 计算方式。
- **重读 u6-l4 ASR**：看一个「为精度牺牲速度」的反例——ASR 构造时关闭所有 fp16/bf16 走纯 fp32，与本讲 decoder 的「激进 bf16」形成对照，加深对精度取舍的理解。
- **重读 u2-l1 基类**：把本讲的 `create_option()` 放回基类公共能力的整体里，理解它与 `load_net`、`sample_logits` 如何共同构成「可继承的运行时底座」。
- **延伸阅读**：若你想深入 ncnn 的 Vulkan 实现，可去 ncnn 上游仓库查阅 `ncnn::Option` 各字段的官方文档与 GPU 算子覆盖情况，理解「为什么某些算子在 GPU 上反而更慢」。
