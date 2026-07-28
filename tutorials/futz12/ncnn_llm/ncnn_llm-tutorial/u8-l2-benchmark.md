# benchmark 性能测试

## 1. 本讲目标

本讲带读者读懂仓库里的性能基准工具 `benchllm`，并学会用它衡量推理速度。读完本讲你应该能够：

- 说清 `benchllm` 的定位——它是一个**解码器（decoder）级别的微基准**，不是端到端的对话跑分；
- 掌握它 6 个位置参数（`loop_count`/`num_threads`/`powersave`/`gpu_device`/`cooling_down`/`seqlen`）的含义与默认值；
- 理解它把一次推理拆成 **prefill（预填充）** 与 **decode（逐 token 解码）** 两阶段分别计时的方法；
- 会把工具打印的**毫秒数**换算成 **tokens/s**、首 token 延迟（TTFT）等工程指标。

> ⚠️ 一个必须先纠正的预期：本讲标题虽然提到「tokens/s」，但 `benchllm` 本身**只打印毫秒**，不直接输出 tokens/s。tokens/s 是读者用工具打印的耗时**自行换算**得到的。这一点贯穿全讲，务必记住。

## 2. 前置知识

在动手之前，先用三段话补齐背景。

**第一，什么是 prefill 与 decode？** 这是 [u2-l3](u2-l3-prefill-flow.md) 与 [u2-l4](u2-l4-generate-loop.md) 已建立的核心概念。一次 LLM 生成分两步：先把用户输入的一整段 prompt（比如 200 个 token）一次性喂进模型，算出每个 token 的 KV cache 并产出**第一个**回复 token——这一步叫 **prefill**，特点是「批量、并行、算量大」；之后每一步只喂上一步生成的 1 个 token，读旧 KV cache、写回新的一行——这一步叫 **decode**，特点是「逐 token、串行、单 token 算量小但反复执行」。两阶段的性能特征完全不同，所以必须分开测。

**第二，微基准（micro-benchmark）与端到端跑分的区别。** 端到端跑分（如跑一段完整对话算 tokens/s）会包含分词、embed 查表、decoder、lm_head、采样、甚至控制台 I/O。而微基准只盯住**最重的算子**——decoder 网络——用「形状正确但数值随意」的输入反复跑，测的是**纯算力吞吐**。`benchllm` 属于后者：它甚至不加载真实权重（用全零权重代替），因为权重数值不影响计算耗时。

**第三，ncnn 的计时单位是毫秒。** 仓库里 `ncnn::sleep(10 * 1000)` 旁边写着注释 `// sleep 10 seconds for cooling down SOC`，10×1000=10000 即 10 秒——这印证了 `ncnn::get_current_time()` 返回的是**毫秒**。本讲所有耗时数字都按毫秒理解。

## 3. 本讲源码地图

本讲只涉及两个文件，加上构建配置里的一行：

| 文件 | 作用 |
| --- | --- |
| [benchmark/benchllm.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp) | 基准工具的全部源码：参数解析、KV cache 槽位发现、prefill/decode 两阶段计时 |
| [readme.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md) | 给出 `xmake run benchllm` 的命令行格式与「LLM benchmark」定位说明 |
| [xmake.lua](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua)（第 87–94 行的 `benchllm` target） | 声明该 target 编译单个 `.cpp`、依赖 `ncnn_llm` 库，并用 `set_rundir` 把运行目录固定到模型目录 |

整个工具是一个**单文件程序**，没有额外头文件，阅读门槛很低。

## 4. 核心概念与源码讲解

### 4.1 benchllm 的定位、rundir 与模型依赖

#### 4.1.1 概念说明

`benchllm` 不是一个「喂 prompt、看模型答什么」的对话工具，而是一个**只测 decoder 网络算力**的微基准。它的设计哲学是：把一切不影响耗时的东西都剥掉——不读真实权重、不算正确性、不做采样——只反复跑 decoder 这一个 ncnn 子网，测它在给定序列长度下的 prefill 与 decode 耗时。

这种设计带来两个直接后果，初学者常踩坑：

1. **它需要一个真实模型的 `.param` 文件，但不需要 `.bin` 权重文件**。因为权重被「空读取器」用全零填充，只有网络结构（`.param`）必须存在。
2. **它的运行目录（rundir）被 xmake 固定死了**，而且被测模型的名字是**写死在源码里**的。换模型要改源码，不是改命令行。

#### 4.1.2 核心流程

```text
启动 main()
  ↓
解析命令行位置参数 → 配置 ncnn Option（线程数/Vulkan）
  ↓
调用 benchmark("minicpm4", 1024, 32, seqlen, opt)   ← 模型名写死
  ↓
load_param("minicpm4_decoder.ncnn.param")            ← 只加载结构
load_model(DataReaderFromEmpty)                      ← 权重填全零
  ↓
扫描 SDPA 算子，自动发现 KV cache 输入输出槽位
  ↓
prefill 阶段：seqlen 个 token，计时
  ↓
decode 阶段：1 个 token，喂入 prefill 产出的 KV cache，计时
```

#### 4.1.3 源码精读

**全零权重读取器**——这是「不需要 `.bin`」的关键。`DataReaderFromEmpty::read` 把权重缓冲区整体清零：

```cpp
// benchmark/benchllm.cpp:12-24
class DataReaderFromEmpty : public ncnn::DataReader
{
public:
    virtual int scan(const char* format, void* p) const { return 0; }
    virtual size_t read(void* buf, size_t size) const
    {
        memset(buf, 0, size);   // 权重全部填 0
        return size;
    }
};
```

含义：它实现了 ncnn 的 `DataReader` 接口，`read` 不读磁盘、直接把缓冲区清零返回。于是 `net.load_model(dr)` 能在**没有 `.bin` 文件**的情况下「加载成功」，所有权重为 0。因为耗时只取决于张量形状与算子结构、与具体数值无关，所以测速完全有效。永久链接：[benchmark/benchllm.cpp:12-24](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L12-L24)。

**被测模型写死**——`main` 末尾只有一次硬编码调用：

```cpp
// benchmark/benchllm.cpp:281
benchmark("minicpm4", 1024, 32, seqlen, opt);
```

含义：模型标识符固定为 `"minicpm4"`，`hidden_size=1024`、`half_embed_dim=32`（即 RoPE 的 `head_dim/2`，对应 `rope_head_dim=64`）。这两个尺寸必须与被测 `.param` 的实际形状一致，否则 `ncnn::Mat` 维度对不上会报错。永久链接：[benchmark/benchllm.cpp:281](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L281)。

**rundir 固定到模型目录**——在 xmake 里：

```lua
-- xmake.lua:87-94
target("benchllm")
    set_kind("binary")
    add_files("benchmark/benchllm.cpp")
    add_deps("ncnn_llm")
    add_packages("ncnn")
    set_rundir("$(projectdir)/assets/minicpm4_0.5b/")
```

含义：`set_rundir` 把 `xmake run benchllm` 时的工作目录设为 `assets/minicpm4_0.5b/`。注意目录名是 `minicpm4_0.5b`（带尺寸后缀），而源码里 `comment` 是 `"minicpm4"`——因此该目录下必须存在一个名为 `minicpm4_decoder.ncnn.param` 的文件（`comment + "_decoder.ncnn.param"`），否则 `load_param` 找不到文件。永久链接：[xmake.lua:87-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L87-L94)。

`.param` 文件的拼接逻辑在 `benchmark` 函数开头：

```cpp
// benchmark/benchllm.cpp:32
std::string param_path = std::string(comment) + "_decoder.ncnn.param";
```

含义：路径相对 rundir，所以必须保证 rundir 下确实有这个文件。若你想换测别的模型，既要改源码里的 `"minicpm4"` 和两个尺寸常量，也要改 xmake 的 `set_rundir` 指向新模型目录。永久链接：[benchmark/benchllm.cpp:32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L32)。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「不需要 `.bin`、但必须要有 `.param`」这一结论。
2. **步骤**：在 `benchmark/benchllm.cpp` 里定位 `load_param(param_path)` 与 `DataReaderFromEmpty dr; net.load_model(dr);` 两处，对照阅读。
3. **现象/预期**：`load_param` 读磁盘文件（缺它会失败），而 `load_model` 读的是 `dr`（全零内存），不碰 `.bin`。
4. **结论**：若把模型目录里的 `.bin` 删掉，`benchllm` 仍应能加载并跑通；但若删掉 `.param`，`load_param` 会报错退出。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么用全零权重测出的耗时仍然可信？
**答案**：因为 decoder 的计算量只依赖张量形状（序列长度、hidden_size、层数）和算子结构（矩阵乘、注意力），与权重、激活的具体数值无关；浮点乘加的耗时对「0」或「任意值」是一样的。

**练习 2**：如果想让 `benchllm` 改测 `qwen3` 模型，至少要改哪几处？
**答案**：① `main` 里 `benchmark("qwen3", hidden, half, seqlen, opt)` 的模型名与两个尺寸常量；② xmake.lua 里 `set_rundir` 指向 `assets/qwen3_xxx/`；③ 确保该目录下有 `qwen3_decoder.ncnn.param`。

---

### 4.2 命令行参数解析

#### 4.2.1 概念说明

`benchllm` 的命令行参数有一个反直觉的特点：它**不用 `--flag` 形式**（对比 [u1-l4](u1-l4-cli-entry-and-options.md) 里 `llm_ncnn_run` 的 `--threads`/`--vulkan`），而是采用**纯位置参数**——第 1 个参数就是 `loop_count`，第 2 个就是 `num_threads`，依此类推。这与 `llm_ncnn_run` 的手写选项解析是两套完全不同的风格。

这 6 个参数控制「测多久」「用多少核」「绑哪个核簇」「用不用 GPU」「要不要等设备降温」「测多长的序列」。

#### 4.2.2 核心流程

```text
main(argc, argv)
  默认值: loop_count=4, num_threads=物理大核数, powersave=2,
          gpu_device=-1, cooling_down=1, seqlen=233
  ↓
按 argc 顺序逐个覆盖: argv[1]→loop_count, argv[2]→num_threads, ...
  ↓
use_vulkan_compute = (gpu_device != -1)
g_enable_cooling_down = (cooling_down != 0)
  ↓
set_cpu_powersave(powersave)        // 绑核策略
set_omp_dynamic(0)                  // 关闭动态线程调配，保证测量稳定
set_omp_num_threads(num_threads)    // OpenMP 线程数
  ↓
opt.num_threads / opt.use_vulkan_compute → 下发到 ncnn::Option
```

#### 4.2.3 源码精读

**默认值与位置覆盖**——`main` 开头给出全部默认值，再按位置覆盖：

```cpp
// benchmark/benchllm.cpp:225-255
int loop_count = 4;
int num_threads = ncnn::get_physical_big_cpu_count();  // 默认取物理大核数
int powersave = 2;
int gpu_device = -1;
int cooling_down = 1;
int seqlen = 233;

if (argc >= 2) loop_count  = atoi(argv[1]);
if (argc >= 3) num_threads = atoi(argv[2]);
if (argc >= 4) powersave   = atoi(argv[3]);
if (argc >= 5) gpu_device  = atoi(argv[4]);
if (argc >= 6) cooling_down= atoi(argv[5]);
if (argc >= 7) seqlen      = atoi(argv[6]);
```

含义：每个参数都有合理默认值，所以 `xmake run benchllm`（不带任何参数）也能跑。注意 `num_threads` 默认不是固定数字，而是 `ncnn::get_physical_big_cpu_count()`——设备有几个物理大核就用几个。永久链接：[benchmark/benchllm.cpp:225-255](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L225-L255)。

**参数到运行环境的翻译**：

```cpp
// benchmark/benchllm.cpp:257-271
bool use_vulkan_compute = gpu_device != -1;     // gpu_device=-1 表示纯 CPU
g_enable_cooling_down = cooling_down != 0;
g_loop_count = loop_count;

ncnn::set_cpu_powersave(powersave);   // 绑核：ncnn 约定 0=全部核 / 1=小核 / 2=大核
ncnn::set_omp_dynamic(0);             // 关掉动态调度，避免线程数抖动
ncnn::set_omp_num_threads(num_threads);

ncnn::Option opt;
opt.num_threads = num_threads;
opt.use_vulkan_compute = use_vulkan_compute;
```

含义：这里有两层线程控制。**ncnn 层**：`opt.num_threads` 决定 ncnn 内部算子并行度；**OpenMP 层**：`set_omp_num_threads` 决定底层矩阵库（若用 OpenMP）的线程数。两者通常设成同一个值。`set_omp_dynamic(0)` 是测量稳定性的关键——不关掉的话，OpenMP 会自己增减线程数，导致每次耗时波动。永久链接：[benchmark/benchllm.cpp:257-271](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L257-L271)。

> **关于 `powersave` 的取值语义**：ncnn 的 `set_cpu_powersave` 接受 0/1/2，分别对应「全部核 / 小核簇 / 大核簇」。默认 2 即绑大核，配合 `get_physical_big_cpu_count` 的线程数，是追求最高算力的常规配置。该语义属 ncnn 公共 API 约定，**可待本地验证**（不同设备核簇划分不同）。

**配置回显**——`main` 会把最终生效的配置全部打印到 stderr，方便核对：

```cpp
// benchmark/benchllm.cpp:273-278
fprintf(stderr, "loop_count = %d\n", g_loop_count);
fprintf(stderr, "num_threads = %d\n", num_threads);
fprintf(stderr, "powersave = %d\n", ncnn::get_cpu_powersave());
fprintf(stderr, "gpu_device = %d\n", gpu_device);
fprintf(stderr, "cooling_down = %d\n", (int)g_enable_cooling_down);
fprintf(stderr, "seqlen = %d\n", seqlen);
```

含义：注意它打印的是 `ncnn::get_cpu_powersave()`（实际生效值），不是输入变量——因为 `set_cpu_powersave` 可能因设备不支持而失败回退。永久链接：[benchmark/benchllm.cpp:273-278](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L273-L278)。

README 给出的调用格式与此一致（[readme.md:218](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L218)）：

```text
xmake run benchllm [loop_count] [num_threads] [powersave] [gpu_device] [cooling_down] [seqlen]
```

#### 4.2.4 代码实践

1. **目标**：验证位置参数确实按顺序覆盖默认值。
2. **步骤**：执行 `xmake build benchllm`；分别跑 `xmake run benchllm`（不带参数）与 `xmake run benchllm 8 2 2 -1 0 128`（依次传 6 个参数）。
3. **观察**：开头回显的 `loop_count / num_threads / seqlen` 是否与传入值一一对应（不带参时应为 `4 / 大核数 / 233`）。
4. **预期**：第二次运行回显应显示 `loop_count = 8`、`num_threads = 2`、`gpu_device = -1`、`cooling_down = 0`、`seqlen = 128`。
5. 若本地未配置模型目录，构建能过、运行阶段会因找不到 `.param` 而失败——这属于 4.1 的依赖问题，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：命令 `xmake run benchllm 16` 只传了一个参数，它修改的是哪个配置？
**答案**：`loop_count`（argv[1]），即正式计时循环跑 16 次，其余参数保持默认。

**练习 2**：为什么作者要显式调用 `set_omp_dynamic(0)`？
**答案**：为了让每次计时的线程数固定不变。若 OpenMP 动态调度开着，它会按系统负载增减线程，导致同一项测试各次耗时大幅波动，失去基准意义。

---

### 4.3 prefill 预热与 generate（decode）计时

#### 4.3.1 概念说明

`benchllm` 把一次推理拆成两个**独立计时**的阶段，对应 [u2-l3](u2-l3-prefill-flow.md) 的 prefill 与 [u2-l4](u2-l4-generate-loop.md) 的 decode 单步：

- **prefill 阶段**：`cur_seqlen = seqlen`（整段）、`past_seqlen = 0`（无历史），一次性算 `seqlen` 个 token，产出 KV cache 与首个 hidden state。
- **decode 阶段**：`cur_seqlen = 1`（单 token）、`past_seqlen = seqlen`（吃 prefill 产出的 cache），模拟「已经生成了若干 token 后的下一步」。

两个阶段都用同一套「**先预热、再计时**」的方法：先用 `g_warmup_loop_count`（默认 8）次跑通流水线（填缓存、拉满频率），再用 `g_loop_count`（默认 4）次正式计时，记录 min/max/avg。

计时前还有一个细节：**cooling down（降温）**。移动端 SoC 在持续高负载后会触发热降频，所以作者在计时前 `sleep` 10 秒让芯片凉下来，避免上一轮的余热污染本次测量。

#### 4.3.2 核心流程

```text
benchmark() 内部：
  ① 扫描所有 SDPA 算子 → 自动收集 KV cache 的输入/输出 blob 下标
  ② 若开启 cooling_down：sleep 10 秒
  ③ prefill 块：
       构造 token_embeds / attention_mask / cos_cache / sin_cache
       warm up 8 次 → 计时 g_loop_count 次 → 打印 min/max/avg
       保存 out_kvcache 供 decode 用
  ④ decode 块：
       把上一步的 KV cache 作为输入喂入
       warm up 8 次 → 计时 g_loop_count 次 → 打印 min/max/avg
```

#### 4.3.3 源码精读

**KV cache 槽位的自动发现**——这是本工具最巧妙的一处。它不靠字符串名（如 [u2-l2](u2-l2-ncnn-pattern-and-text-runtime.md) 共享运行时用的 `cache_k%d`），而是**遍历 ncnn 网络里所有 SDPA（缩放点积注意力）算子**，找输出数为 3 的，把其最后两个输入/输出当作 KV cache：

```cpp
// benchmark/benchllm.cpp:44-63
for (size_t i = 0; i < net.layers().size(); i++)
{
    const ncnn::Layer* op = net.layers()[i];
    if (op->typeindex != ncnn::LayerType::SDPA)   // 只看注意力算子
        continue;

    const size_t input_count = op->bottoms.size();
    const size_t output_count = op->tops.size();

    if (output_count == 3)                          // 3 个输出 = 含 KV cache 出口
    {
        kv_cache_indexes.push_back(op->bottoms[input_count - 2]);     // K 入
        kv_cache_indexes.push_back(op->bottoms[input_count - 1]);     // V 入
        out_kv_cache_indexes.push_back(op->tops[output_count - 2]);   // K 出
        out_kv_cache_indexes.push_back(op->tops[output_count - 1]);   // V 出
    }
}
```

含义：解码器导出时，每层注意力的 `tops` 末尾两个就是更新后的 K/V cache（这是导出脚本的隐式约定）。这段代码自动把所有层的 cache 槽位下标收集成数组，于是 prefill 时只需把输出 cache 全 `extract` 出来、decode 时把它们作为输入喂回去。永久链接：[benchmark/benchllm.cpp:44-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L44-L63)。

**降温**：

```cpp
// benchmark/benchllm.cpp:66-70
if (g_enable_cooling_down)
{
    // sleep 10 seconds for cooling down SOC  :(
    ncnn::sleep(10 * 1000);
}
```

含义：`ncnn::sleep` 单位是毫秒，`10*1000`=10 秒。注释里那个 `:(` 表情是作者对移动端热降频的无奈——但为了数据可比，不得不等。永久链接：[benchmark/benchllm.cpp:66-70](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L66-L70)。

**prefill 阶段的张量形状**——注意与 [u2-l3](u2-l3-prefill-flow.md) 的掩码形状呼应：

```cpp
// benchmark/benchllm.cpp:76-82
const int cur_seqlen = seqlen;       // 整段
const int past_seqlen = 0;           // 无历史
ncnn::Mat token_embeds(hidden_size, cur_seqlen);                // (hidden, seqlen)
ncnn::Mat attention_mask(past_seqlen + cur_seqlen, cur_seqlen); // (seqlen, seqlen) 因果方阵
ncnn::Mat cos_cache(half_embed_dim, cur_seqlen);
ncnn::Mat sin_cache(half_embed_dim, cur_seqlen);
```

含义：`attention_mask` 是 `(seqlen, seqlen)` 的方阵——正是 [u2-l3](u2-l3-prefill-flow.md) 讲的因果掩码形状（主体 N-1 段的方阵）。`cos_cache`/`sin_cache` 每个位置 `half_embed_dim` 个值（= `rope_head_dim/2`）。永久链接：[benchmark/benchllm.cpp:76-82](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L76-L82)。

**预热 + 计时的标准三段式**（以 prefill 为例）：

```cpp
// benchmark/benchllm.cpp:88-141（节选）
// warm up
for (int i = 0; i < g_warmup_loop_count; i++) {     // 8 次：填缓存、拉频率
    ncnn::Extractor ex = net.create_extractor();
    ex.input("in0", token_embeds); ex.input("in1", attention_mask);
    ex.input("in2", cos_cache);   ex.input("in3", sin_cache);
    /* extract KV cache + out0 */
}
// 计时
double time_min = DBL_MAX, time_max = -DBL_MAX, time_avg = 0;
for (int i = 0; i < g_loop_count; i++) {             // 4 次：正式测量
    double start = ncnn::get_current_time();         // 毫秒
    { /* 同上一次 extractor */ }
    double end = ncnn::get_current_time();
    double time = end - start;
    time_min = std::min(time_min, time);
    time_max = std::max(time_max, time);
    time_avg += time;
}
time_avg /= g_loop_count;
fprintf(stderr, "%20s (prefill)  min = %7.2f  max = %7.2f  avg = %7.2f\n",
        comment, time_min, time_max, time_avg);
```

含义：min/max/avg 三者一起打印是有意的——avg 看典型水平，min/max 反映波动范围；若 min 与 max 差很多，说明测量不稳定（可能没关 `omp_dynamic` 或没降温）。注意 `ncnn::get_current_time()` 返回毫秒，所以打印值单位是**毫秒**。永久链接：[benchmark/benchllm.cpp:88-141](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L88-L141)。

**decode 阶段**——形状退化为单 token，并把 prefill 的 cache 喂回去：

```cpp
// benchmark/benchllm.cpp:148-172（节选）
const int cur_seqlen = 1;            // 单 token
const int past_seqlen = seqlen;      // 历史 KV 长度 = prefill 的序列长
ncnn::Mat token_embeds(hidden_size, cur_seqlen);                 // (hidden, 1)
ncnn::Mat attention_mask(past_seqlen + cur_seqlen, cur_seqlen);  // (seqlen+1, 1) 列向量
...
// 把 prefill 产出的 KV cache 作为输入喂入
for (size_t i = 0; i < kv_cache_indexes.size(); i++)
{
    ex.input(kv_cache_indexes[i], kvcache[i]);     // cache_k%d / cache_v%d
}
```

含义：这里 `attention_mask` 是 `(seqlen+1, 1)` 的**列向量**——与 [u2-l4](u2-l4-generate-loop.md) 讲的「decode 阶段 mask 全 0、因为当前 token 恒为序列末尾」完全对应。decode 把上一步保存的 `kvcache`（`kvcache = out_kvcache`，[benchmark/benchllm.cpp:143](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L143)）作为输入喂回去，模拟真实的「读旧 cache、写新一行」。永久链接：[benchmark/benchllm.cpp:148-172](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L148-L172)。

decode 的计时与 prefill 完全同构，只是打印行多了 `(decode)` 标记（[benchmark/benchllm.cpp:219](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L219)）。

#### 4.3.4 代码实践

1. **目标**：理解「预热 8 次、计时 4 次」的必要性。
2. **步骤**：阅读 [benchmark/benchllm.cpp:88-137](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L88-L137)，找到 warm up 循环与计时循环的分界；再读 [benchmark/benchllm.cpp:26-28](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L26-L28) 的全局变量定义。
3. **思考实验**：如果把 warm up 循环去掉，min/max/avg 会如何变化？
4. **预期**：去掉预热后，第一次正式计时通常明显偏大（CPU 频率没拉满、缓存是冷的），导致 max 偏高、avg 偏高。
5. 若想本地验证，可临时把 `g_warmup_loop_count` 改成 0 重新编译对比（**仅用于实验，勿提交**）。

#### 4.3.5 小练习与答案

**练习 1**：prefill 与 decode 两阶段的 `attention_mask` 形状分别是什么？为什么不同？
**答案**：prefill 是 `(seqlen, seqlen)` 方阵（因果掩码，屏蔽未来 token）；decode 是 `(seqlen+1, 1)` 列向量（当前 token 恒为序列末尾，因果性天然满足，无需屏蔽）。这与 u2-l3/u2-l4 讲的掩码逻辑一致。

**练习 2**：为什么 decode 阶段要先 `kvcache = out_kvcache` 再喂回输入？
**答案**：因为 KV cache 是 prefill 的产物，decode 单步必须读入「历史所有 token 的 K/V」才能正确计算注意力；benchllm 用 prefill 输出的 cache 作为 decode 输入，模拟真实自回归解码。

---

### 4.4 从毫秒到 tokens/s：性能指标换算

#### 4.4.1 概念说明

`benchllm` 只打印 `min/max/avg` 三个**毫秒数**，工程师最关心的 **tokens/s（每秒生成多少 token）** 与 **TTFT（首 token 延迟）** 需要自己换算。换算很简单，但必须搞清两阶段的「token 数」对应关系：

- prefill 处理的 token 数 = `seqlen`（整段输入）。
- decode 处理的 token 数 = `1`（每步一个）。

注意 `benchllm` 测的是 **decoder 子网**，不含 lm_head 与采样——而这两者在真实推理里很轻量，所以 decoder 耗时是 TTFT 与 decode 速度的良好**近似**，但不完全相等。

#### 4.4.2 核心流程

设打印出的 prefill 平均耗时为 \( t_{\text{prefill}} \) 毫秒、decode 平均耗时为 \( t_{\text{decode}} \) 毫秒，序列长度为 \( s \)（即 `seqlen`），则有：

- **首 token 延迟（TTFT，近似）**：

\[ \text{TTFT} \approx t_{\text{prefill}} \quad (\text{单位：ms}) \]

- **prefill 吞吐**（一次性处理 \( s \) 个 token）：

\[ \text{prefill\_tokens\_per\_s} = \frac{s \times 1000}{t_{\text{prefill}}} \]

- **decode 吞吐**（每步 1 个 token）：

\[ \text{decode\_tokens\_per\_s} = \frac{1000}{t_{\text{decode}}} \]

> 1000 的来源：1 秒 = 1000 毫秒，把「token/毫秒」乘 1000 得「token/秒」。

举一个**假设数字**（非真实测量，仅演示换算）：若 `seqlen=233`、`t_prefill=200ms`、`t_decode=15ms`，则 TTFT≈200ms，prefill 吞吐 ≈ 233×1000/200 ≈ 1165 tokens/s，decode 吞吐 ≈ 1000/15 ≈ 66.7 tokens/s。可以看到 prefill 因批量并行、吞吐远高于 decode——这正是为什么要分两阶段测。

#### 4.4.3 源码精读

本模块没有新的源码段落，而是把 [4.3](#43-prefill-预热与-generate-decode计时) 的两行打印作为换算入口。关键的两处 fprintf：

prefill 输出（[benchmark/benchllm.cpp:141](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L141)）：

```cpp
fprintf(stderr, "%20s (prefill)  min = %7.2f  max = %7.2f  avg = %7.2f\n", comment, time_min, time_max, time_avg);
```

decode 输出（[benchmark/benchllm.cpp:219](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp#L219)）：

```cpp
fprintf(stderr, "%20s  (decode)  min = %7.2f  max = %7.2f  avg = %7.2f\n", comment, time_min, time_max, time_avg);
```

含义：两行的 `avg` 就分别是公式里的 \( t_{\text{prefill}} \) 与 \( t_{\text{decode}} \)。`%7.2f` 保留两位小数，单位毫秒。永久链接见上。

需要强调的**诚实结论**：源码里**没有任何一行计算或打印 tokens/s**。如果你在别处看到「benchllm 输出 tokens/s」，那是误传或基于改版；上游版本只给毫秒，换算靠人。

#### 4.4.4 代码实践

1. **目标**：亲手把毫秒换算成 tokens/s 与 TTFT。
2. **步骤**：运行 `benchllm` 拿到两行 `(prefill)` 与 `(decode)` 的 avg 值；记录本次的 `seqlen`。
3. **换算**：代入上面三个公式，算出 TTFT、prefill tokens/s、decode tokens/s。
4. **观察**：改变 `seqlen`（如 128 / 233 / 512），prefill tokens/s 与 decode tokens/s 哪个更稳定？prefill 耗时是否近似随 `seqlen` 线性增长？
5. **预期**：decode tokens/s 对 `seqlen` 不太敏感（每步只算 1 个 token，主要受 `past_seqlen` 影响的注意力开销）；prefill 耗时随 `seqlen` 增大显著上升。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：某次跑分 `seqlen=512`，prefill avg=400ms，decode avg=20ms。求 TTFT 与两个 tokens/s。
**答案**：TTFT≈400ms；prefill 吞吐 = 512×1000/400 = 1280 tokens/s；decode 吞吐 = 1000/20 = 50 tokens/s。

**练习 2**：为什么 benchllm 测出的 TTFT 只是「近似」而非真实首 token 延迟？
**答案**：benchllm 只测 decoder 子网，真实 TTFT 还包括 embed 查表、lm_head 投影、采样选 token 等步骤。不过这些相对 decoder 的算量很小，所以 decoder 耗时是 TTFT 的主要部分与良好近似。

---

## 5. 综合实践

把本讲的参数解析、两阶段计时、指标换算串起来，做一组对照实验。

**任务**：在已准备好 `assets/minicpm4_0.5b/`（含 `minicpm4_decoder.ncnn.param`）的环境下，运行下列 4 组配置，记录每组的回显配置、prefill avg、decode avg，并换算出 TTFT 与 decode tokens/s：

| 组别 | 命令 | 关注点 |
| --- | --- | --- |
| A | `xmake run benchllm` | 全默认基线 |
| B | `xmake run benchllm 8 1 2 -1 1 233` | 只用 1 线程，看并行收益 |
| C | `xmake run benchllm 4 <大核数> 2 -1 1 512` | 加长 seqlen，看 prefill 是否线性变慢 |
| D | `xmake run benchllm 4 <大核数> 2 0 1 233` | 开启 Vulkan（`gpu_device=0`），对比 GPU/CPU |

**要求**：

1. 把每组结果填入一张表（列：loop_count / num_threads / seqlen / prefill avg / decode avg / TTFT / decode tokens/s）。
2. 回答两个问题：
   - B 与 A 比，线程数从「大核数」降到 1，decode tokens/s 大约降到几分之一？这就是该模型 decode 阶段的并行度。
   - C 与 A 比，seqlen 从 233 涨到 512，prefill avg 是否近似翻倍？这反映 prefill 的计算量与序列长度的关系（注意力部分是 \( O(s^2) \)，但小序列下矩阵乘的 \( O(s) \) 部分也很显著）。
3. **安全提醒**：第 D 组要求编译期 ncnn 已开 Vulkan（xmake 的 `configs.vulkan=true`，见 [u1-l2](u1-l2-build-and-run.md)），且本机有可用的 Vulkan 设备；若不可用就跳过，并记录「GPU 不可用」。

> 若本地无模型目录，本任务无法跑出真实数字——此时请改为**源码阅读型**：通读 `benchmark/benchllm.cpp` 全文，画出「main 解析参数 → benchmark() 发现 KV 槽位 → prefill 计时 → decode 计时」的调用图，并标注每个全局变量（`g_warmup_loop_count`/`g_loop_count`/`g_enable_cooling_down`）在哪里被读、哪里被写。

## 6. 本讲小结

- `benchllm` 是**解码器级微基准**，只测 `*_decoder.ncnn.param` 这一个子网，用 `DataReaderFromEmpty` 喂全零权重、不需要 `.bin`，输入也是 dummy 张量——目的是测纯算力而非正确性。
- 被测模型**写死**为 `benchmark("minicpm4", 1024, 32, seqlen, opt)`，运行目录由 xmake 的 `set_rundir` 固定到 `assets/minicpm4_0.5b/`；换模型要同时改源码与 xmake。
- 命令行是**位置参数**（非 `--flag`）：`[loop_count] [num_threads] [powersave] [gpu_device] [cooling_down] [seqlen]`，默认值依次为 `4 / 物理大核数 / 2 / -1 / 1 / 233`。
- 推理被拆成 **prefill**（`cur_seqlen=seqlen`、`past_seqlen=0`）与 **decode**（`cur_seqlen=1`、`past_seqlen=seqlen`）两阶段，各自走「预热 8 次 + 计时 4 次」的标准三段式，KV cache 槽位通过**扫描 SDPA 算子**自动发现。
- 工具只打印**毫秒**（min/max/avg），**不直接输出 tokens/s**；TTFT≈prefill avg，prefill 吞吐 = \( s\times1000/t_{\text{prefill}} \)，decode 吞吐 = \( 1000/t_{\text{decode}} \)。
- 计时前可选 `sleep` 10 秒降温（应对移动端热降频），`set_omp_dynamic(0)` 关动态调度保稳定——两者都是基准可信的细节保障。

## 7. 下一步学习建议

- 想看「真正的端到端 tokens/s」（含分词、embed、lm_head、采样）如何产生，可对照 [u2-l4](u2-l4-generate-loop.md) 的 generate 主循环，思考：若在 generate 里加 `get_current_time()` 计时，与 benchllm 的 decode 计时差在哪。
- 想理解 decoder 为何能开 `use_bf16_storage` 而 fp16 算术被关，进入 [u8-l1](u8-l1-vulkan-threads.md)（Vulkan/线程/精度配置），它与本讲的 `opt.num_threads`/`use_vulkan_compute` 是上下游关系。
- 想为 `benchllm` 加上 tokens/s 自动换算或支持多模型，可作为二次开发练手——参考 [u8-l6](u8-l6-add-new-model.md) 的改动清单思路，把硬编码的模型名改成读 `model.json`。
- 继续阅读 [benchmark/benchllm.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/benchmark/benchllm.cpp) 全文（仅 285 行），尝试在本机跑通一次并填出综合实践的表格。
