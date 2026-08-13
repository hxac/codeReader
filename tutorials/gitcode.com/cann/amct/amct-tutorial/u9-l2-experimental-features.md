# 实验特性与前沿模型量化

## 1. 本讲目标

本讲聚焦 AMCT 仓库里一个「常被忽略却又很关键」的角落——`amct_pytorch/experimental/`。AMCT 主流程（前几讲讲的 eval / extract_ptq_data / ptq / deploy 四条 CLI）是稳定、通用的量化骨架；而 `experimental` 子包放的是另一类东西：面向**尚未稳定的前沿模型 / 尚未进入主硬件栈的数据类型 / 尚未合入 NPU 算子栈的低比特格式**的试验性实现。

学完本讲，读者应能做到：

- 说清 `experimental` 子包的定位，以及为什么它默认**不进**发布包、必须用 `--experimental` 构建开关才会被打包。
- 读懂 DeepSeek-V4 的离线权重转换工具 `convert_model.py`，理解「FP8/MXFP4 → INT8/BF16」这条不走 PTQ、纯格式转换的路径。
- 对照 `examples/models/` 下的两个一站式样例（DeepSeek-V4-Flash、Qwen3.6-MoE），看懂它们如何把主流程四条命令串起来跑通一个真实前沿模型。

本讲是专家层的「扩展实践」课，承接 u1-l4（四条 CLI）、u4-l4（deploy 导出）建立的主流程认知，重点在「主流程之外」的实验性分支。

## 2. 前置知识

本讲默认读者已经掌握以下概念（来自前置讲义）：

- **PTQ 四阶段链路**（u1-l4）：`eval → extract_ptq_data → ptq → deploy`，靠 `data_dir` 与 `*_param_dir` 接力。
- **deploy 的两种粒度**（u4-l4）：`block` 模式逐层烘焙量化参数；`tensor` 模式不做 PTQ，按源分片做**纯格式转换**（如 FP8→bf16 反量化）。
- **低比特数据类型**（u2-l2）：INT8/INT4 为整数均匀格点；MXFP8/MXFP4 是 Microscaling 浮点，沿权重 -1 轴每 32 个元素**共享一个指数**；FP8 为 8-bit 浮点。
- **compressed-tensors 量化配置格式**（u4-l4）：`config.json` 里的 `quantization_config` 字段，含 `quant_method`、`format`（int-quantized / float-quantized）、`ignore`、`config_groups` 等。

本讲会用到几个新术语，先在此统一解释：

- **实验特性（experimental）**：接口、实现、依赖或目标硬件尚未稳定，未来可能调整甚至重构，因此隔离在独立子包、默认不打包。
- **离线权重转换（offline weight conversion）**：不跑量化训练、不在 NPU 上前向，只在 CPU 上读写 `safetensors` 文件、把权重从一种低比特格式直接转成另一种（或反量化回浮点）。
- **一站式平台**：厂商预置好的 Atlas A3 单卡运行环境，用户无需自行拉 docker、装驱动，CANN 路径固定为 `/home/developer/Ascend/cann`。

## 3. 本讲源码地图

本讲涉及的关键文件与作用：

| 文件 | 作用 |
|------|------|
| `build.sh` | 顶层构建脚本，定义 `--experimental` 开关 |
| `setup.py` | 打包脚本，按 `AMCT_EXPERIMENTAL` 环境变量决定是否包含 experimental 子包 |
| `amct_pytorch/CMakeLists.txt` | cmake 配置，把开关透传为环境变量 |
| `amct_pytorch/experimental/__init__.py` | experimental 包标记（空文件） |
| `amct_pytorch/experimental/deepseek-v4/README.md` | DeepSeek-V4 转换工具说明 |
| `amct_pytorch/experimental/deepseek-v4/convert_model.py` | 离线权重转换主脚本 |
| `amct_pytorch/experimental/deepseek-v4/convert_config.py` | 生成 compressed-tensors 量化配置 |
| `amct_pytorch/experimental/deepseek-v4/mx_quantize.py` | MX 格式量化 / 打包工具（依赖 torchao） |
| `amct_pytorch/experimental/hifloat8/README.md` | HiFloat8 伪量化模块说明 |
| `amct_pytorch/experimental/fakequant/README.md` | MXFP4 伪量化算子说明 |
| `examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md` | DeepSeek-V4-Flash 端到端 PTQ 演示 |
| `examples/models/qwen3.6/Qwen3.6-Moe.md` | Qwen3.6-MoE 一站式平台量化指南 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **experimental 子包定位与 `--experimental` 构建开关**——回答「它是什么、为什么默认不打包、开关怎么传」。
2. **DeepSeek-V4 离线权重转换工具**——回答「不走主流程的另一种量化路径长什么样」。
3. **一站式平台样例**——回答「真实前沿模型怎么用主流程跑通」。

### 4.1 experimental 子包定位与 --experimental 构建开关

#### 4.1.1 概念说明

`amct_pytorch/experimental/` 是 AMCT 的「试验场」。它收纳了几类暂时不适合进主包的能力：

- **前沿模型量化样例**：如 `experimental/quantization/DeepSeekV3.2/`、`experimental/deepseek-v4/`，针对特定新模型，往往自带一套不依赖主框架的脚本（`main.py` / `deploy.py` / `eval.py`）。
- **未入主栈的数据类型 / 伪量化算子**：如 `experimental/hifloat8/`（CPU + OpenMP 实现的 HiFloat8 伪量化）、`experimental/fakequant/mxfp4_ascendc/`（MXFP4 伪量化算子）。
- **独立算法移植**：如 `experimental/flatquant/`（FlatQuant 的 NPU 移植版，与主流程 `algorithms/quant/flatquant.py` 是两套并存的实现）。

它们的共性是：**接口可能变、依赖可能重（如 torchao、onnx）、目标硬件可能尚未普及**。AMCT 的策略是——默认构建时把这些代码**排除在发布包之外**，只服务最稳定的 LLM PTQ 主流程；需要用的人必须显式打开 `--experimental`。

> 术语：**发布包（wheel / sdist）**是 `pip install` 装的那个压缩包；**experimental 子包**默认不在其中。注意区分「源码里有」和「装上后有」——源码里永远有，装上后默认没有。

#### 4.1.2 核心流程

`--experimental` 从命令行到最终「是否打包」要经过**四跳**传递，这是理解整套机制的关键：

```
build.sh --experimental                    # 第 1 跳：shell 变量 ENABLE_EXPERIMENTAL=TRUE
        │
        ▼  (拼到 CMAKE_ARGS)
cmake -DENABLE_EXPERIMENTAL=TRUE           # 第 2 跳：cmake 变量
        │
        ▼  (CMakeLists.txt 里 export)
环境变量 AMCT_EXPERIMENTAL=TRUE             # 第 3 跳：进程环境变量
        │
        ▼  (setup.py 里 os.getenv)
find_packages 包含 amct_pytorch.experimental  # 第 4 跳：打包时是否纳入
```

为什么要四跳？因为构建过程横跨 shell → cmake → Python 三个工具链，每个工具链有自己的变量作用域，必须靠「环境变量」这种跨语言的中介一节节传递。这跟 u1-l2 讲过的「构建选项经多跳决定打包行为」是同一种设计。

#### 4.1.3 源码精读

**第 1 跳**：`build.sh` 解析命令行参数，把 `--experimental` 翻成 shell 变量。

[build.sh:97-100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L97-L100) —— shell 变量 `ENABLE_EXPERIMENTAL=TRUE` 在此被置位。

**第 2 跳**：`build.sh` 把该变量拼进 cmake 参数。

[build.sh:233](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/build.sh#L233) —— `CMAKE_ARGS` 追加 `-DENABLE_EXPERIMENTAL=${ENABLE_EXPERIMENTAL}`，把 shell 变量交给 cmake。

**第 3 跳**：cmake 执行打包命令前，把 cmake 变量导出成环境变量。

[amct_pytorch/CMakeLists.txt:36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/CMakeLists.txt#L36) —— `export AMCT_EXPERIMENTAL=${ENABLE_EXPERIMENTAL}`，把 cmake 变量透传给即将启动的 Python 打包进程的环境。

**第 4 跳**：`setup.py` 读环境变量，决定 `find_packages` 是否包含 experimental。

[setup.py:44-55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L44-L55) —— 默认（未设环境变量）走 `else` 分支，用 `exclude=['amct_pytorch.experimental', 'amct_pytorch.experimental.*']` 把整个试验子包排除；只有 `AMCT_EXPERIMENTAL=TRUE` 时才走 `if` 分支全量包含。关键判定在 [setup.py:46](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/setup.py#L46) 这一行。

子包入口本身只是个空标记文件：[amct_pytorch/experimental/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/__init__.py)（空文件），它存在的意义只是让 `amct_pytorch.experimental` 成为一个合法 Python 包，供 `find_packages` 发现其下的子模块。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「默认构建排除 experimental、加 `--experimental` 才包含」这件事。

**操作步骤**（源码阅读型实践，无需真正编译）：

1. 打开 `setup.py` 第 44–55 行，对照下面的伪代码理解两个分支：

   ```python
   # 伪代码（示例代码，非项目原文件，仅示意 set_packages 的等价逻辑）
   if os.getenv('AMCT_EXPERIMENTAL', '').upper() == 'TRUE':
       packages = find_packages(include=['amct_pytorch', 'amct_pytorch.*'])          # 全包含
   else:
       packages = find_packages(include=['amct_pytorch', 'amct_pytorch.*'],
                                exclude=['amct_pytorch.experimental', '...*'])       # 排除试验
   ```

2. 打开 `build.sh`，搜索 `--experimental`，跟踪它如何流到 `CMAKE_ARGS`。
3. 打开 `amct_pytorch/CMakeLists.txt` 第 36 行附近，确认 `export AMCT_EXPERIMENTAL`。

**需要观察的现象 / 预期结果**：

- 默认（不带 `--experimental`）构建出的 tar.gz，解压后**不应**出现 `amct_pytorch/experimental/` 目录。
- 带 `bash build.sh --torch --experimental` 构建出的包，解压后**应**包含 `experimental/`。

> 待本地验证：上述两条结论需实际执行构建并解压产物确认；本实践只做了静态阅读。

#### 4.1.5 小练习与答案

**练习 1**：如果不执行 `build.sh`，而是直接在仓库根目录跑 `python setup.py sdist`，experimental 会被打包吗？

**参考答案**：取决于当前 shell 的 `AMCT_EXPERIMENTAL` 环境变量。`setup.py` 只看环境变量、不看 `build.sh`；直接跑 `setup.py` 时若没设 `AMCT_EXPERIMENTAL=TRUE`，则走排除分支，experimental 不打包。这也说明四跳链中任何一跳断了都会回退到「不打包」。

**练习 2**：为什么 experimental 默认不打包？请给出两条理由。

**参考答案**：① 接口 / 实现不稳定，进主包会破坏版本兼容承诺；② 部分 experimental 模块引入重依赖（如 `deepseek-v4/requirements.txt` 里的 `torchao`、hifloat8 的 C++ 扩展），默认打包会拖累所有用户的安装体积与依赖链。

---

### 4.2 DeepSeek-V4 离线权重转换工具

#### 4.2.1 概念说明

DeepSeek-V4 是一个「混合精度」模型：**注意力（attention）模块用 FP8，MoE 专家模块用 MXFP4**。问题在于——**Atlas A2、Atlas A3 系列硬件不支持 FP8 / MXFP4**（见 [amct_pytorch/experimental/deepseek-v4/README.md:3-5](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/README.md#L3-L5)）。

于是 `convert_model.py` 提供了一条**不走 PTQ、不碰 NPU** 的捷径：在 CPU 上直接把权重从 FP8/MXFP4 转成 INT8（或反量化成 BF16），让 DeepSeek-V4 能在 A2/A3 上跑起来。这条路径与主流程四条 CLI 完全无关——它不评估精度、不提取校准数据、不训练，只是「按张量读写、换格式、再落盘」。

> 重要区分：`convert_model.py` 是**离线格式转换**（CPU、无训练）；u4 讲的 `ptq` 是**在线逐层重建训练**（NPU、有反向）。两者都能产出低比特权重，但代价与精度完全不同。

转换支持的四种目标格式（见 [README:32-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/README.md#L32-L39)）：

| `quant_type` | 含义 | 输出格式 |
|------|------|---------|
| `bfloat16` | 反量化、不重新量化 | BF16 |
| `w8a8-int` | W8A8 整数量化（默认） | INT8 + scale |
| `w8a8-mx` | W8A8 MX 浮点量化 | MXFP8 + scale |
| `w4a8-mx` | W4A8 MX 浮点量化 | MXFP4 + scale |

#### 4.2.2 核心流程

`main()` 的处理逻辑可概括为「先全量反量化到 BF16，再按需重新量化」：

```
读 model.safetensors.index.json + config.json
        │
        ▼
按 quant_type 解析标志位：w4a8 / w8a8 / mx
        │
        ▼
generate_quant_layers()   → 决定「哪些层要重新量化、量化到几位」
generate_quant_config()   → 生成 compressed-tensors 的 quantization_config（写进 config.json）
        │
        ▼
遍历每个 *.safetensors 文件：
  for 每个 weight 张量：
    ├── 后缀 .scale            → 跳过（处理对应 weight 时按需读取）
    ├── element_size()==1（1 字节）：
    │     ├── dtype==int8       → MXFP4：解包(low/high 4bit)→反量化(block=32)→BF16
    │     └── 其它(FP8)         → 直接按 128×128 块反量化→BF16
    │     （此时 weight 已是 BF16）
    │     └── 若层名∈quant_layers → 重新量化：
    │           ├── mx 分支      → quantize_mx()（+ 4bit 再 pack_uint4）
    │           └── int 分支     → int_weight_quant()（per-channel INT8）
    └── 其它（已是非量化层）     → 原样保留
        │
        ▼
逐文件 save_file 落盘 + LRU 缓存（只留最近 2 个文件控内存）
        │
        ▼
copy_py_json 复制 .py/.json/.jinja + 重写 index.json + 重写 config.json
```

关键判断在 `element_size() == 1`：FP8 与 MXFP4 都按 1 字节存储（MXFP4 是两个 4-bit 值打包进一个 uint8），所以用 `element_size()` 先粗筛，再用 `dtype` 细分（`int8` 说明是打包的 MXFP4，浮点 dtype 说明是 FP8）。

重新量化时的位宽由 `generate_quant_layers()` 决定：MoE 专家在 `w4a8` 时降到 4-bit，其余默认 8-bit；attention 的部分投影在 `mx` 模式下才纳入量化（见下一节源码）。

#### 4.2.3 源码精读

**反量化函数 `weight_dequant`**：把带 `scale` 的低比特权重还原成浮点，核心是「scale 广播到 weight 同形状后逐元素相乘」。

[convert_model.py:39-92](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L39-L92) —— FP8 走 128×128 块（`repeat_interleave` 行列各扩 128 倍），MX 走 per-32-element（只沿列扩 32 倍，对应 Microscaling 的 shared exponent）。数学上即：

\[
w_{\text{dequant}} = w_{\text{quant}} \odot \mathrm{broadcast}(s)
\]

**MXFP4 解包 `unpack_mxfloat4_to_fp32`**：把一个 uint8 拆成两个 4-bit 索引，查 16 值的 E2M1 表得到浮点值，元素数量翻倍。

[convert_model.py:95-128](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L95-L128) —— `low_4bits = packed & 0x0F`、`high_4bits = (packed // 16) & 0x0F`，再用 16 个固定浮点值（`[0, 0.5, 1, 1.5, 2, 3, 4, 6, ...]` 及其负值）的查找表把索引映射回 FP32。这正是 u2-l2 讲的「FP4_E2M1 仅 16 个非均匀值」的工程落地。

**per-channel INT8 量化 `int_weight_quant`**：对称、逐通道。

[convert_model.py:131-141](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L131-L141) —— 沿 dim=1 取绝对值最大值算 scale，公式为：

\[
s = \frac{\max_{j}|w_{ij}|}{q_{\max}}, \quad q_{\max} = 2^{b-1}-1, \quad \hat{w} = \mathrm{clip}(\mathrm{round}(w/s),\ -q_{\max},\ q_{\max})
\]

这跟 u2-l1 讲的「权重 per-channel 对称量化」完全一致——权重是静态的，可离线算 scale。

**量化层位宽表 `generate_quant_layers`**：决定每个待量化层的目标位数。

[convert_model.py:144-181](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L144-L181) —— `moe_bit = 4 if w4a8 else 8`；attention 的 `wq_a/wkv/wo_a` 仅在 `is_mx` 时纳入；`indexer.wq_b` 仅在该层 `compress_ratios==4` 时纳入。返回的字典 `{层名: bit}` 驱动主循环里的重新量化分支。

**主循环的重新量化分支**：先反量化到 BF16，再按 `quant_layers` 决定是否重新量化。

[convert_model.py:300-347](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L300-L347) —— `mx` 分支调 `quantize_mx()`，4-bit 时再 `f32_to_f4_unpacked` + `pack_uint4` 打包回 MXFP4；`int` 分支调 `int_weight_quant()`。新 scale 写到 `.weight` → `.scale` 的改名 key 下。

**compressed-tensors 配置生成**：产出推理引擎能识别的 `quantization_config`。

[convert_config.py:90-119](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_config.py#L90-L119) —— `format` 字段：`is_fp` 为真写 `float-quantized`、否则 `int-quantized`；`group_0` 目标 `Linear`、`group_1`（仅 MX）目标 `MoEGMM`，这与 u4-l4 讲的 deploy `quantization_config` 格式一脉相承。`generate_ignore_item`（[convert_config.py:27-53](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_config.py#L27-L53)）则列出词嵌入、输出头、compressor 等**不量化**的层。

**命令行入口**：

[convert_model.py:369-388](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L369-L388) —— `--input_fp8_hf_path` / `--output_hf_path` / `--quant_type`（默认 `w8a8-int`，注意 argparse `choices` 只列了三个，但 `main()` 内部断言支持四个）。

> 补充：`experimental/quantization/DeepSeekV3.2/` 是**另一个**独立样例（注意是 V3.2，不是 V4），它自带 `main.py`/`deploy.py`/`eval.py` 与逐 block 量化学习接口（[README:6-7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/quantization/DeepSeekV3.2/README.md#L6-L7)），不依赖主框架，是「sample 式」的参考实现，与主流程 CLI 无交集。

#### 4.2.4 代码实践

**实践目标**：理解 `convert_model.py` 的「反量化 → 重新量化」两段式，并能解释 `element_size()==1` 的筛选作用。

**操作步骤**（源码阅读型实践）：

1. 阅读 [convert_model.py:300-325](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L300-L325)，画出三种张量（`.scale` / 1 字节 / 其它）的分流图。
2. 阅读 [convert_model.py:326-347](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/deepseek-v4/convert_model.py#L326-L347)，回答：一个 MoE 专家的 `w1` 权重，在 `--quant_type w4a8-mx` 下，会经历哪几步变换？

**预期结果**（第 2 步）：原始是打包的 MXFP4（`dtype=int8`、1 字节）→ `unpack_mxfloat4_to_fp32` 解包成 FP32（元素翻倍）→ `weight_dequant`（`block_size=32, is_mx=True`）反量化成 BF16 → 命中 `quant_layers`（`moe_bit=4`）→ `quantize_mx(weight, 4, real_quant=True)` 重新量化 → `f32_to_f4_unpacked` + `pack_uint4` 打包回 MXFP4。即「MXFP4 → BF16 → MXFP4」的往返，但 scale 是按目标格式重新算的。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `element_size() == 1` 而不是 `dtype == torch.float8_e4m3fn` 来筛选 FP8？

**参考答案**：因为待转换的权重既有 FP8（浮点 dtype）也有 MXFP4（`dtype=int8`，是打包的），两者**每个元素都占 1 字节**。`element_size()` 先把这两类「需要解包 / 反量化」的低比特权重一起捞出，再用 `dtype` 在内部细分（`int8` → MXFP4 走解包路径，浮点 → FP8 走块反量化路径）。若只按 FP8 dtype 筛，会漏掉 MXFP4。

**练习 2**：`convert_model.py` 和主流程的 `deploy`（tensor 模式）都会做 FP8→BF16，二者有什么本质区别？

**参考答案**：`convert_model.py` 是**独立 CPU 脚本**，只读写 `safetensors`、不加载模型、不需要 NPU，还能进一步重新量化成 INT8/MXFP；而 `deploy` 的 tensor 模式（u4-l4）走的是**主流程 Workflow**，会加载模型适配器、按源分片转换、并生成标准 `quantization_config`。前者轻量、针对 DeepSeek-V4 硬编码层名；后者通用、走注册表与模型适配器。

---

### 4.3 一站式平台样例：DeepSeek-V4-Flash 端到端 PTQ 与 Qwen3.6-MoE

#### 4.3.1 概念说明

如果说 4.2 的 `convert_model.py` 是「不走主流程」的捷径，那么 `examples/models/` 下的两个样例则是「**正经走主流程**」把前沿模型跑通的范本。它们用到的就是 u1-l4 讲的四条 CLI，但针对具体模型给出了完整可复制的参数。

两个样例的定位：

- **DeepSeek-V4-Flash Walkthrough**：一个用 Python 代码片段驱动的端到端演示，每步用 `show_cmd` 打印命令（`dry_run=True` 默认不真跑），切换 `run_cmd(..., dry_run=False)` 才执行。它把四条命令按 eval → extract_ptq_data → ptq → deploy 串起来。
- **Qwen3.6-MoE**：面向 Atlas A3 一站式平台的实操指南，给出可直接粘贴的 shell 命令，宣称 BF16 与 A8W4 量化下 PPL 掉点在 0.1 以内。

> 注意：这两个样例用的是**主流程 CLI**，模型适配器（`deepseek_v4`、`qwen3_6_moe`）已经合入主包，所以**不需要** `--experimental` 开关就能跑。`experimental` 子包里的 `deepseek-v4/convert_model.py` 是另一条独立的转换路径，别混淆。

#### 4.3.2 核心流程

**DeepSeek-V4-Flash Walkthrough 的八步**（见 [DeepSeekV4-Flash-Walkthrough.md:5-17](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L5-L17) 总览）：

| 步骤 | 命令 | 作用 |
|------|------|------|
| 1. 环境准备 | （Python 检查） | 验证 torch / transformers 可用 |
| 2. 权重准备 | `deploy.py`（granularity=tensor, eval_mode=bf16） | **先把 FP8+FP4 转 BF16**（关键预处理） |
| 3. 基准评测 | `eval`（eval_mode=bf16） | 浮点 PPL 基线 |
| 4. 直转评测 | `eval`（eval_mode=quant，无 algos） | 不训练、直接量化的精度损失参考 |
| 5. 校准数据 | `extract_ptq_data`（逐 quant_target） | 录制每个待量化模块的输入激活 |
| 6. PTQ | `ptq`（逐 quant_target，algos=lwc） | 训练量化参数 |
| 7. 带参评测 | `eval`（eval_mode=quant + `*_param_dir`） | 验证 PTQ 是否改善了精度 |
| 8. 导出 | `deploy`（带 `*_param_dir`） | 烘焙可部署权重 |

这里有一个**承接 u4-l4 的关键点**：DeepSeek-V4-Flash 官方权重是混合 FP8+FP4，而「当前代码仓所有流程都基于 bfloat16」（[Walkthrough:50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L50)），所以第 2 步先用 deploy 的 **tensor 模式**把权重反量化成 BF16——这正是 u4-l4 讲的「tensor 模式做纯格式转换（FP8→bf16）」的典型用法。

**Qwen3.6-MoE 的标准七步**（见 [Qwen3.6-Moe.md:52-181](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/qwen3.6/Qwen3.6-Moe.md#L52-L181)）：基准测试 → 直转量化评测 → PTQ 数据提取 → PTQ（autoround）→ 带参直转评测 → 量化权重导出。它用 `granularity block`、`model_name qwen3_6_moe`、`bit_config w4a8.yaml`，是一套更贴近生产环境的 shell 流程。

#### 4.3.3 源码精读

**模型适配器已注册进主包**：两个样例用的 `model_name` 都在主流程注册表里，无需 experimental。

[amct_pytorch/common/models/llm/__init__.py:27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L27) —— `from .deepseek.deepseek_v4.deepseekv4 import DeepseekV4`，注册 `deepseek_v4`。

[amct_pytorch/common/models/llm/__init__.py:32](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L32) —— `from .qwen.qwen3_6.qwen3_6_moe import Qwen3_6Moe`，注册 `qwen3_6_moe`。这正是 u5-l2 讲的「适配器靠 import 副作用登记进 MODEL_REGISTRY」。

**DeepSeek-V4 第 2 步：tensor 模式转 BF16**：

[DeepSeekV4-Flash-Walkthrough.md:122-133](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L122-L133) —— `deploy.py` 配 `--granularity tensor --eval_mode bf16`，对应 u4-l4 讲的「tensor 粒度做 FP8→bf16 反量化」。

**DeepSeek-V4 第 5/6 步：逐 quant_target 提取与训练**：

[DeepSeekV4-Flash-Walkthrough.md:191-222](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L191-L222) —— 用 Python 循环为 `PTQ_TARGETS = ["moe"]` 里每个 target 单独构造 `extract_ptq_data` 命令。注意注释明确：「`extract_ptq_data.py` 只支持单个 `quant_target`，因此本节会为每个 `quant_target` 分别构建指令」——这与 u4-l1 讲的「extract 与 ptq 的 quant_target 必须一致、每次只能填一个」完全呼应。

[DeepSeekV4-Flash-Walkthrough.md:241-278](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L241-L278) —— ptq 阶段同理逐 target 构造命令，并用 `--moe_mlp_param_dir` / `--attn_linear_param_dir` / `--attn_cache_param_dir` 把训练好的参数存到约定目录，供第 7、8 步复用。这是 u1-l4 讲的「`*_param_dir` 在 ptq↔deploy 间接力」的真实落地。

**Qwen3.6-MoE 一站式平台说明**：

[Qwen3.6-Moe.md:19-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/qwen3.6/Qwen3.6-Moe.md#L19-L28) —— 一站式平台是预置 Atlas A3 单卡环境，CANN 路径固定 `/home/developer/Ascend/cann`，无需 docker。

[Qwen3.6-Moe.md:117-135](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/qwen3.6/Qwen3.6-Moe.md#L117-L135) —— PTQ 用 `--algos autoround --base_lr 1e-3 --epochs 10`，这是 u6-2 讲的 autoround（weight target）算法的真实用法。

> 一个值得注意的细节：DeepSeek-V4-Flash Walkthrough 全程用 `GRANULARITY = "tensor"`（[Walkthrough:67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md#L67)），而 Qwen3.6-MoE 用 `granularity block`（[Qwen3.6-Moe.md:62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/qwen3.6/Qwen3.6-Moe.md#L62)）。granularity 的选择与模型规模、是否需要格式转换有关——DeepSeek-V4 这类需先做 FP8→bf16 转换的超大模型更适合 tensor；标准 block-wise PTQ 则用 block。**待本地验证**：具体某模型某 target 该用哪种 granularity，以最新官方文档与本地实测为准。

#### 4.3.4 代码实践

**实践目标**：梳理在 Atlas A3 一站式平台上完成 DeepSeek-V4 量化的标准启动步骤，并解释 experimental 的隔离必要性。

**操作步骤**：

1. 完整阅读 [examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md)，把第 1–8 步的命令按顺序抄成一张清单，标注每步「产出什么、给下一步用什么」。例如第 5 步产出 `calib_output_root/<target>/` 的校准 pkl，供第 6 步 ptq 的 `--data_dir` 读取。
2. 对照 [Qwen3.6-Moe.md:19-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/models/qwen3.6/Qwen3.6-Moe.md#L19-L28) 的「一站式平台指南」，说明在一站式平台上跑这些命令时，docker 步骤要怎么跳过、`cann_path` 要改成什么。
3. 回答本讲的核心问题：**为什么 DeepSeek-V4 这类特性要放在 experimental 且需要 `--experimental` 开关？**

**需要观察的现象 / 预期结果**：

- 八步命令严格按 eval → extract_ptq_data → ptq → deploy 的依赖顺序排列，其中第 2 步（deploy tensor 转 bf16）是 DeepSeek-V4 特有的预处理。
- 一站式平台跳过 docker 拉起，`cann_path` 用 `/home/developer/Ascend/cann`。
- 关于第 3 问（见下方练习答案）。

> 待本地验证：实际启动命令需在有 DeepSeek-V4-Flash 权重与 Atlas A3 环境的机器上验证；本实践为文档阅读型。

#### 4.3.5 小练习与答案

**练习 1**（对应实践第 3 问）：为什么这些前沿特性要放在 `experimental` 且需要 `--experimental` 构建开关？

**参考答案**：三个原因。① **目标硬件尚未普及**——DeepSeek-V4 的 FP8/MXFP4 在 A2/A3 上不支持，相关转换工具只服务少数早期用户，进主包会误导大众以为它是稳定能力；② **依赖重且易变**——`deepseek-v4/requirements.txt` 额外依赖 `torchao`，hifloat8 需 C++ 扩展，默认打包会拖累所有用户；③ **接口未稳定**——experimental 模块的层名、参数、量化格式可能随硬件能力演进调整（如 [fakequant/README.md:22-24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/experimental/fakequant/README.md#L22-L24) 明说「接口与实现可能随硬件能力演进而调整」）。`--experimental` 开关让普通用户装到稳定包、让前沿用户显式 opt-in 承担不稳定风险。

**练习 2**：DeepSeek-V4-Flash Walkthrough 用主流程 CLI，为什么还需要 experimental 子包里的 `convert_model.py`？

**参考答案**：两条路径服务不同场景。主流程 CLI（Walkthrough）需要加载完整模型适配器、在 NPU 上跑、走 tensor/block 逐层处理，适合做完整 PTQ 与精度评估；而 `convert_model.py` 是**纯 CPU 离线脚本**，不加载模型、不需要 NPU，适合「快速把权重转成 A2/A3 能跑的格式」这一最小需求。后者更轻、更快，但精度不如前者（无 PTQ 训练）。

---

## 5. 综合实践

设计一个把本讲三个模块串起来的小任务：**为一个新的「混合精度前沿模型」规划量化落地方案**。

假设有一个虚构模型 `X-Model`，它的 attention 用 FP8、FFN 用 INT8、词嵌入为 BF16，目标是在 Atlas A3 上部署。请完成：

1. **判断路径**：参考本讲，列出两条可选路径——(A) 用类似 `convert_model.py` 的离线转换、(B) 用主流程四条 CLI 走 PTQ——并说明各自需要什么前提（CPU/NPU、是否需校准数据、精度预期）。
2. **构建开关**：若决定把针对 X-Model 的转换脚本放进仓库，应该放在哪个目录？为什么默认构建要排除它？写出 `build.sh` 让它进包的命令。
3. **命令串联**：参考 DeepSeek-V4-Flash Walkthrough 的八步，为 X-Model 写出主流程的命令顺序草图（仅命令名 + 关键参数，不需真实路径），标出哪一步做 FP8→BF16 转换、哪几步之间靠 `data_dir` / `*_param_dir` 接力。

**参考思路**：

- 路径 A 放 `amct_pytorch/experimental/<x-model>/convert_model.py`，CPU 即可、无需校准数据、精度一般；路径 B 需 NPU、需校准数据、精度更好。
- 放 `experimental` 是因为接口未稳定 + 重依赖；命令是 `bash build.sh --torch --experimental`。
- 八步顺序与 4.3.2 的表一致；FP8→BF16 转换在第 2 步（deploy tensor）；`data_dir` 在 extract→ptq 间接力、`*_param_dir` 在 ptq→(eval/deploy) 间接力。

> 待本地验证：综合实践为方案设计型，不涉及真实运行。

## 6. 本讲小结

- `amct_pytorch/experimental/` 是 AMCT 的试验场，收纳前沿模型样例、未入主栈的数据类型 / 伪量化算子、独立算法移植；**默认不进发布包**。
- `--experimental` 开关经 **shell 变量 → cmake 变量 → 环境变量 → setup.py 的 `find_packages`** 四跳，决定是否打包 experimental 子包；任何一跳断了都回退到「不打包」。
- `deepseek-v4/convert_model.py` 是一条**不走 PTQ、纯 CPU 离线**的权重格式转换路径，把 FP8/MXFP4 转成 INT8/BF16，解决 DeepSeek-V4 在 A2/A3 上不支持 FP8/MXFP4 的问题；核心是「反量化到 BF16 → 按 `quant_layers` 重新量化」。
- `element_size()==1` 粗筛 + `dtype` 细分，是区分 FP8 与打包 MXFP4 的关键技巧；MXFP4 解包后元素翻倍、查 16 值 E2M1 表。
- `examples/models/` 下的 DeepSeek-V4-Flash 与 Qwen3.6-MoE 两个样例**走主流程 CLI**，模型适配器已在主包注册（`deepseek_v4`、`qwen3_6_moe`），按 eval→extract→ptq→deploy 串联，靠 `data_dir` 与 `*_param_dir` 接力。
- experimental 的隔离是务实取舍：让普通用户装稳定包、让前沿用户显式 opt-in；接口、依赖、目标硬件任一不稳定都应先进 experimental。

## 7. 下一步学习建议

- 若对 NPU 算子底层感兴趣，可结合 u8-l1～u8-l3 阅读 `experimental/fakequant/mxfp4_ascendc/`——它刻意对齐了 `amct_ops/hifloat8_cast` 的三层结构（op_kernel / op_extension / python），是「实验性算子未来如何迁入 `amct_ops/`」的样板。
- 若想深入 FlatQuant 的两套实现，可对比 `experimental/flatquant/`（NPU 移植版）与主流程的 `amct_pytorch/algorithms/quant/flatquant.py`（u6-4 讲的可学习算法版），理解「同一算法的试验版与稳定版差异」。
- 若关心测试与质量保障，下一篇 u9-l3 会讲 AMCT 的测试体系、pytest 标记（cpu/npu/slow）与 CI，可了解 experimental 特性如何被纳入或排除在测试覆盖之外。
- 想跑通真实样例的读者，建议先在 Atlas A3 一站式平台上照 Qwen3.6-MoE 指南走一遍标准七步，再回头挑战 DeepSeek-V4-Flash 的八步（多一步 FP8→BF16 转换）。
