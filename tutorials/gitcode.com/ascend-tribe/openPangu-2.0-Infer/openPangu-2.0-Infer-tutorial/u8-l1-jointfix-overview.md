# W8A8 量化与 jointfix 工具架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 **W8A8 量化**是什么：权重 per-channel 静态量化 + 激活 per-token 动态量化，为什么它能给 openPangu-2.0 带来约 1.9× 的体积压缩。
2. 画出 jointfix 的 **core / backends / methods 三层正交架构**：加新模型只动 `backends/`，加新量化方法只动 `methods/`，互不影响。
3. 解释 **registry 模式**：`--backend pangu --method jointfix` 这两个字符串是如何经过装饰器注册表变成 Python 类的。
4. 读懂 **CLI 入口** `jointfix quantize / finalize` 两个子命令的分工，以及「method 自己往 CLI 注参数」的两遍解析技巧。
5. 说清 NPU 上 `pip install -e . --no-deps` 的避坑原因——为什么让 pip 碰 torch 会「废掉整台机器的 NPU 后端」。

本讲是 jointfix 单元（u8）的第一讲，只讲「工具怎么组织」；量化算法本身的精读（(a,b) 联合搜索、GPTQ、坐标下降）留给 u8-l3，部署产物组装留给 u8-l4。

## 2. 前置知识

### 2.1 为什么需要量化

大模型推理的最大瓶颈往往不是算力，而是**显存容量与显存带宽**。openPangu-2.0 这类 MoE 模型的权重默认以 BF16（每个参数 2 字节）存储，505B 参数仅权重就要上 TB 级存储。把权重从 16 位降到 8 位，理想情况下权重体积减半、搬运带宽减半，这就是**量化**（quantization）的动机。

**训练后量化（PTQ, Post-Training Quantization）**是与「量化感知训练（QAT）」相对的概念：PTQ 不重新训练模型，只拿几百条校准数据在已有 BF16 权重上「量一下」，几十分钟到几小时就能产出可部署的低精度模型。jointfix 就是一个 PTQ 工具箱。

### 2.2 量化的两个正交维度：按谁分组、何时定标

一个 int8 数只能表示 \([-127, 127]\) 的整数，而神经网络里的浮点数范围千差万别，所以量化必须配一个**缩放因子（scale）**。scale 怎么算、对谁共用，有两个正交维度：

| 维度 | 含义 | jointfix 的选择 |
|------|------|----------------|
| **粒度**（strategy） | 一个 scale 管多大范围 | 权重 **per-output-channel**（一列权重共享一个 scale）；激活 **per-token**（一行输入共享一个 scale） |
| **时机**（dynamic） | scale 何时确定 | 权重**静态**（量化时离线算好，固化进文件）；激活**动态**（推理时对每个 batch 现场算） |

对线性层 \(y = Wx\)，W8A8 的计算近似为：

\[
y \approx s_w \, s_a \; (W_q \cdot x_q), \quad W_q = \mathrm{round}(W / s_w), \; x_q = \mathrm{round}(x / s_a)
\]

其中 \(s_w\) 形状为 \([(\text{out},1)]\)（每输出通道一个），\(s_a\) 形状按 token 维展开（每个 token 一个）。整数矩阵乘法在 NPU 上有专用加速通路，这就是 W8A8 的收益来源。

### 2.3 注册表模式与 entry point

- **注册表（registry）模式**：用一个 `{名字: 类}` 的字典 + 装饰器，把「字符串参数」与「具体实现类」解耦。你在 u2-l4 见过 PatchManager 的补丁注册，思想相同：新实现 = 新文件 + 一行装饰器，调用方代码零改动。
- **console script entry point**：`pyproject.toml` 里的 `[project.scripts]` 声明「命令名 = 模块:函数」，pip 安装后自动生成可执行命令。这与 u2-l1 讲的 `vllm.platform_plugins` entry point 机制同源——发现靠包元数据，加载靠「模块:属性」字符串。

## 3. 本讲源码地图

jointfix 位于仓库 `tools/quant/jointfix/`（注意：仓库里有两层 `jointfix`，外层是项目根，内层是 Python 包）：

| 文件 | 作用 |
|------|------|
| [tools/quant/jointfix/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md) | 工具定位、架构图、安装与生产流程 |
| [tools/quant/jointfix/pyproject.toml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/pyproject.toml) | 依赖声明与 `jointfix` 命令入口 |
| [tools/quant/jointfix/jointfix/cli.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py) | 命令行入口：`quantize` / `finalize` 两个子命令 |
| [tools/quant/jointfix/jointfix/registry.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/registry.py) | 名字 → 类的两个注册表 |
| [tools/quant/jointfix/jointfix/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/__init__.py) | 包入口：import 即触发注册 |
| [tools/quant/jointfix/jointfix/backends/\_\_init\_\_.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/__init__.py) 与 [methods/\_\_init\_\_.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/__init__.py) | 内置 backend / method 的导入与注册 |
| [tools/quant/jointfix/jointfix/backends/base.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/base.py) | `ModelBackend` 抽象基类（模型轴接口） |
| [tools/quant/jointfix/jointfix/methods/base.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/base.py) | `QuantMethod` 抽象基类（方法轴接口） |
| [tools/quant/jointfix/tests/test_cli.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/tests/test_cli.py) | 纯 CPU 的 CLI 行为测试（本讲综合实践要用） |

---

## 4. 核心概念与源码讲解

### 4.1 PTQ 基础与 W8A8 产物

#### 4.1.1 概念说明

jointfix 的自我定位写在 README 第一段：**多模型、方法可插拔的 INT8（W8A8）训练后量化（PTQ）工具箱**，面向昇腾 NPU 上的大规模 MoE 模型。它解决的问题是：输入一个 BF16 模型，输出一个 **vLLM 可直接加载**的 W8A8 compressed-tensors 模型。

「W8A8」= Weight 8 bit + Activation 8 bit。与之相对的是 W8A16（只量化权重）——更省显存但吃不满 int8 算力。jointfix 选择 W8A8，权重做 **per-output-channel 静态量化**，激活做 **per-token 动态量化**：权重是固定的，离线把 scale 一起写进权重文件；激活随输入变化，推理时现场统计。

为什么要「平滑参数搜索」（JointFix 方法）？因为权重和激活里都存在**离群通道**（少数绝对值特别大的维度），它们会把共享 scale 撑大，导致其余正常值量化后挤在 0 附近、精度崩塌。JointFix 的解法是对每个线性层联合搜索最优的 \((a,b)\) 平滑参数，把难度在权重侧和激活侧之间重新分配——这是 u8-l3 的主题，本讲只需知道「方法藏在 methods 轴里」。

#### 4.1.2 核心流程

一次完整量化（`quantize` 子命令）的宏观流程：

```text
BF16 模型 + 校准集
   │
   ├─ 1. cli 解析参数，registry 解析 --backend/--method 为类
   ├─ 2. backend 枚举 decoder 层（layer_specs）
   ├─ 3. runner 逐层循环：
   │      BF16 前向收激活统计 → method 搜索 (a,b) 并量化
   │      → 存 layer_*.safetensors → 量化后重新前向验证误差
   └─ 4. finalize 自动组装：RTN 兜底未标定层 + 写 config.json
   │
   ▼
W8A8 模型目录（vLLM 免 --quantization 直接加载）
```

README 用一句话概括这张图（原文见下方源码精读第 2 条）：「一次量化 = cli 解析参数 → registry 选出 backend + method → runner 逐层做『BF16 前向收统计 → method 搜索+量化 → 存盘 → 量化后重新前向传误差』」。

#### 4.1.3 源码精读

**① 工具定位与压缩收益**——[tools/quant/jointfix/README.md:1-9](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md#L1-L9)

README 开头声明：输入 BF16 模型、输出 vLLM 可直接加载的 W8A8 compressed-tensors 模型，体积压缩约 **1.9×**（不是理论上的 2×，因为 scale 与少量保留 BF16 的层占了开销），推理精度基本无损。第 8 行的一行流程图 `BF16 模型 ──jointfix quantize──▶ W8A8 可部署模型 ──▶ vLLM-Ascend` 就是整个工具的存在意义。

**② 方法概要与量化粒度**——[tools/quant/jointfix/README.md:11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md#L11)

这一行给出核心方法 JointFix 的四要素：联合搜索最优 \((a,b)\) 平滑参数、Hessian 通道加权、K=2 坐标下降迭代、「输出侧 GPTQ + 输入侧 RTN」混合量化；并明确写出本讲的关键定义——**「权重做 per-output-channel 静态量化，激活做 per-token 动态量化」**。

**③ 产物契约：quantization_config**——[tools/quant/jointfix/README.md:143-168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md#L143-L168)

量化完成后 finalize 会在输出目录 `config.json` 写入 `quantization_config`，这是工具与 vLLM 之间的**交付契约**：

- `quant_method: "compressed-tensors"` + `quantize: "w8a8_dynamic"` —— vLLM 据此自动选择量化实现，**无需**命令行加 `--quantization`；
- `weights: {strategy: "channel", dynamic: false}` —— 权重 per-channel 静态；
- `input_activations: {strategy: "token", dynamic: true}` —— 激活 per-token 动态；
- `ignore: [...]` —— 跳过量化的层名列表（自动生成），这些层保持 BF16。

回忆 u1-l3：92B 的 w8a8 ansible 模板部署的正是这种模型，且「计算精度仍为 bfloat16」——量化只作用于权重与激活的表示，层间计算与累加仍在高精度进行。

#### 4.1.4 代码实践

**实践目标**：不跑真量化，只用纸笔 + 目录观察确认 W8A8 的「静态权重 scale」概念。

1. 打开 [tools/quant/jointfix/README.md:143-166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md#L143-L166)，抄下 `config_groups.group_0` 的完整 JSON。
2. 逐字段标注：哪个字段说明权重静态？哪个字段说明激活动态？`num_bits` 是多少？
3. 用公式估算：一个 \(4096 \times 1024\) 的 BF16 权重矩阵（8 MB）量化为 int8 + per-channel scale（4096 个 fp16 scale，约 8 KB）后体积是多少？（答案约 4 MB + 8 KB，压缩比约 1.98×，与 README 的 1.9× 吻合。）
4. 思考：如果激活也做成静态（`dynamic: false`），需要额外存什么？为什么 LLM 输入分布多变时静态激活 scale 更伤精度？

**预期结果**：能独立说出「weights 块的两个关键字段是 `strategy: channel` + `dynamic: false`」。第 4 步为开放思考，**待本地验证**（可与 u8-l4 实测精度对比联动）。

#### 4.1.5 小练习与答案

**练习 1**：W8A8 中的两个 8 分别指什么？为什么不叫 W8？

答案：分别指 Weight 8 bit 与 Activation 8 bit。只量化权重叫 weight-only（W8A16），W8A8 让矩阵乘法的两个操作数都是 int8，才能吃满 NPU 的 int8 算力通路。

**练习 2**：「per-channel 静态」与「per-token 动态」这四个词两两组合，jointfix 为什么选这一组而不是「per-tensor 静态激活」？

答案：权重在部署后不变，可以离线精算每个输出通道的 scale（静态、per-channel，粒度细、误差小）；激活随每个请求变化，无法离线确定，只能推理时对每个 token 现场统计（动态、per-token）。per-tensor 激活会让整层共用一个 scale，离群 token 会拖垮所有正常 token 的精度。

**练习 3**：为什么 `ignore` 列表里的层要保持 BF16 而不强行量化？

答案：例如共享专家每 token 必经（README 关键参数表 `--skip-shared-experts` 一行），其量化误差会全局累积；又如 1 维的 norm 权重本就无法按通道量化。跳过它们以极小的体积代价换回显著精度。

---

### 4.2 registry 模式：名字到类的解析

#### 4.2.1 概念说明

CLI 上用户写的是字符串 `--backend pangu --method jointfix`，而 Python 需要的是类。registry 模式用一个模块级字典承接这层翻译：

- **backend 是「模型」轴**：封装「某个模型怎么读权重、怎么搭一层前向、哪些层跳过量化」，与用什么量化算法无关；
- **method 是「方法」轴**：封装「某个量化算法怎么变换和量化一层」，与具体模型无关；
- 两条轴由与双方都无关的 `core/`（runner、统计、原语）粘合。

这个设计的价值是**正交扩展**：加新模型只动 `backends/`，加新方法（未来的 quarot/spinquant/awq）只动 `methods/`，`core/` 与 CLI 主干零改动。对照 u2-l4 的 PatchManager：那边注册的是「对 vLLM 的补丁」，这边注册的是「自家工具的插件」，机制同构。

#### 4.2.2 核心流程

注册与解析的完整链路（**import 即注册**）：

```text
import jointfix                      # cli.py 第 14 行
  └─ jointfix/__init__.py
       ├─ import backends ──► backends/__init__.py
       │     ├─ import hf    ──► @register_backend("hf")     装饰器执行，写入 _BACKENDS
       │     └─ import pangu ──► @register_backend("pangu")  装饰器执行，写入 _BACKENDS
       └─ import methods ──► methods/__init__.py
             └─ import jointfix ──► @register_method("jointfix") 写入 _METHODS

运行期：
  --backend pangu   ──► get_backend("pangu")  ──► _BACKENDS["pangu"] ──► 类
  --method jointfix ──► get_method("jointfix") ──► _METHODS["jointfix"] ──► 类
```

关键点：装饰器在 **import 时**执行副作用（往字典写条目），所以「谁被 import」决定「谁被注册」——这就是 `__init__.py` 必须逐个 import 子模块、且 `cli.py` 必须先 `import jointfix` 的原因。

#### 4.2.3 源码精读

**① 注册表本体：两个字典 + 两对装饰器/取值函数**——[tools/quant/jointfix/jointfix/registry.py:15-50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/registry.py#L15-L50)

`_BACKENDS` 与 `_METHODS` 是两个模块级私有字典（L15-16）；`register_backend` / `register_method`（L19-30）是装饰器工厂——外层函数收名字，内层 `deco` 收类、写字典、原样返回类；`get_backend` / `get_method`（L33-42）按键取类，名字不存在时抛 `KeyError` 并附上已注册名单（报错信息即排障信息）；`available_backends` / `available_methods`（L45-50）返回排序后的键列表，直接喂给 argparse 的 `choices`。

**② 注册的触发点：包入口 import 即注册**——[tools/quant/jointfix/jointfix/\_\_init\_\_.py:15-17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/__init__.py#L15-L17)

这两行带注释 `Trigger registry population (decorators run on import)` 的 import 是整个机制的扳机：import `jointfix` 包就连带 import `backends`、`methods` 两个子包，装饰器随之执行。`cli.py` 第 14 行的 `import jointfix  # noqa: F401  (populates the registries)` 正是依赖这一行为——不 import 就查无此类。

**③ 内置注册项**——[tools/quant/jointfix/jointfix/backends/\_\_init\_\_.py:3-5](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/__init__.py#L3-L5)、[tools/quant/jointfix/jointfix/methods/\_\_init\_\_.py:3-4](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/__init__.py#L3-L4)

backends 子包 import 了 `hf`（实验性）与 `pangu`（生产）两个实现；methods 子包目前只有 `jointfix` 一个实现。装饰器落点分别在 [jointfix/backends/pangu.py:83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L83)（`@register_backend("pangu")`）、[jointfix/backends/hf.py:23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/hf.py#L23)（`@register_backend("hf")`）、[jointfix/methods/jointfix.py:274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L274)（`@register_method("jointfix")`，其下 L275-277 定义 `JointFixMethod`，`needs_activations = True` 表示该方法需要校准前向）。

**④ 两条轴的接口契约（基类）**——[tools/quant/jointfix/jointfix/backends/base.py:36-56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/base.py#L36-L56)、[tools/quant/jointfix/jointfix/methods/base.py:19-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/base.py#L19-L33)

`ModelBackend` 的 docstring 写明隔离方向：「runner 与 methods 只和这个接口打交道，从不 import 具体模型实现；加新模型 = 加一个子类，core/ 和 methods/ 一行不改」。抽象方法覆盖层枚举（`layer_specs`）、跳过模式（`skip_patterns`）、权重读写、层前向构建。`QuantMethod` 则以 `add_cli_args` / `configure` / `process_layer` 等钩子约定了方法轴的生命周期，并预留 `needs_activations` 开关——weight-only 的旋转类方法（quarot/spinquant）可置 False 让 runner 跳过统计。

#### 4.2.4 代码实践

**实践目标**：在纯 CPU 环境（无需 NPU、无需真权重）验证 registry 内容与注册链路。

1. 进入工具根目录（注意 README 的提醒：是外层 `tools/quant/jointfix`，不要 cd 进内层同名包目录），执行 `pip install -e .`。
2. 运行：

   ```bash
   python -c "import jointfix; from jointfix.registry import available_backends, available_methods; print(available_backends(), available_methods())"
   ```

3. 再故意验证「import 即注册」：只用 `from jointfix.registry import available_backends`（不 import `jointfix` 包本体）打印一次，对比两次输出。

**需要观察的现象**：第 2 步应打印 `['hf', 'pangu'] ['jointfix']`（**待本地验证**）；第 3 步若只 import `registry` 子模块，装饰器未触发，两个列表应为空 `[] []`（Python 的包 import 机制会先初始化父包 `jointfix/__init__.py`，而父包恰好 import 了 backends/methods——请思考为什么实践中列表可能仍非空，并据此理解「注册发生在 `__init__.py` 的 import 副作用里」这一事实）。

**预期结果**：能口头回答「`--backend pangu` 的字符串经过 `get_backend` 查 `_BACKENDS` 字典得到类」。

#### 4.2.5 小练习与答案

**练习 1**：想支持一个新模型，最少要写哪些代码？

答案：在 `backends/` 下新建文件，继承 `ModelBackend` 实现全部抽象方法，类上标 `@register_backend("my-model")`，然后在 `backends/__init__.py` 加一行 import。CLI、registry、core、methods 全都不用改——`--backend my-model` 立即可用。

**练习 2**：`get_backend("foo")` 抛 KeyError 时，为什么报错里还要打印 `registered: [...]`？

答案：registry.py L35 的 f-string 故意带上已注册名单，让用户第一时间看到拼写正确选项。这是 registry 工具的通用友好实践——CLI 侧 argparse 的 `choices` 校验会更早拦截，但库被直接调用时报错信息就是唯一线索。

**练习 3**：`QuantMethod.needs_activations = False` 的方法（如 quarot）会让 runner 跳过什么？

答案：跳过校准前向的激活统计收集（methods/base.py L25-28 的注释写明 weight-only rotation 方法可置 False）。这是方法轴反向定制 core 行为的一个钩子，体现两轴虽正交、但通过接口上的可选元信息通信。

---

### 4.3 CLI 子命令与 NPU 安装避坑

#### 4.3.1 概念说明

`jointfix` 命令有两个子命令，对应量化流水线的两段：

- **`quantize`**：完整流程——逐层校准+量化，且**默认一步到底**：跑完逐层循环后自动执行 finalize，直接产出 vLLM 可加载模型。加 `--no-finalize` 可停在逐层中间产物 `layer_*.safetensors`。
- **`finalize`**：仅做组装——把已有的逐层产物（配合 `--no-finalize` 或中断的跑批）拼成 compressed-tensors 模型，对校准循环从未访问、没有逐层文件的权重用 RTN 兜底量化。

CLI 的设计难点在于：**方法轴的参数（如 `--objective`、`--num-iterations`）不该写死在 cli.py 里**，否则加方法就得改 CLI，破坏正交性。jointfix 的解法是「method 自己往 parser 注入参数组」+ 一次对 `sys.argv` 的预扫描。

安装侧最大的坑在 NPU：`pyproject.toml` 把 `torch>=2.1` 列为硬依赖，而 NPU 机器上的 torch/torch_npu 由 CANN 套件提供。若直接 `pip install -e .`，一旦 CANN 的 torch 版本号不满足约束，pip 会从 PyPI 拉 CPU 版 torch **覆盖掉** torch_npu，NPU 后端直接废掉——所以 NPU 上必须 `--no-deps`。

#### 4.3.2 核心流程

`main()` 的分派逻辑（伪代码）：

```text
parse_args (build_parser)
├─ cmd == "finalize":
│    backend = get_backend(args.backend)(args.model)
│    finalize_model(...)                    # 只组装
└─ cmd == "quantize":
     backend = get_backend(args.backend)(args.model)     # 实例化模型轴
     method  = get_method(args.method)()                 # 实例化方法轴
     method.configure(args)                              # 方法参数落到方法配置
     calibrate_and_quantize(backend, method, RunConfig)  # core 主循环
     _finalize_deploy(...)                               # 默认自动组装（--no-finalize 跳过）
```

两遍解析（two-pass）技巧：argparse 构建 parser 时就需要知道方法参数，但 `--method` 的值要到 parse 才知道。于是 `_peek(["--method"])` 先裸扫一遍 `sys.argv` 拿到方法名，再让该方法 `add_cli_args(q)` 注入自己的参数组，最后才真正 `parse_args`。

#### 4.3.3 源码精读

**① 命令入口声明**——[tools/quant/jointfix/pyproject.toml:23-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/pyproject.toml#L23-L24)

`jointfix = "jointfix.cli:main"` 声明 console script：pip 安装后生成的 `jointfix` 命令等价于调用 `jointfix.cli` 模块的 `main()` 函数（README L65 也写明 `jointfix ...` 等价于 `python -m jointfix.cli ...`）。依赖声明在同文件 [L11-18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/pyproject.toml#L11-L18)，注意第一项就是惹祸的 `torch>=2.1`。

**② 子命令骨架与 registry 联动**——[tools/quant/jointfix/jointfix/cli.py:21-30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L21-L30)

`build_parser` 建立必需子命令（`required=True`），`quantize` 的 `--backend` / `--method` 直接用 `choices=available_backends()` / `available_methods()` 作合法值——**注册表内容实时反映到命令行帮助里**，加新 backend 后帮助文档自动更新。这一段能跑的前提是文件头 [L14-17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L14-L17) 的 `import jointfix` 已把注册表填好。

**③ 方法参数注入（两遍解析）**——[tools/quant/jointfix/jointfix/cli.py:42-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L42-L45) 与 [L64-73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L64-L73)

注释 `# Let the chosen method inject its own argument group.` 点明设计意图；`_peek` 裸扫 `sys.argv` 支持 `--method jointfix` 与 `--method=jointfix` 两种写法。注入的实现在方法侧：[jointfix/methods/jointfix.py:295-309](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L295-L309) 的 `add_cli_args` 添加名为 `jointfix: joint (a,b) search` 的参数组，含 `--objective`、`--write-quant`、`--num-iterations`、`--iter-ab-tol`、`--skip-shared-experts` 等 10 余个方法专属参数——cli.py 里一个都没有。文件头 docstring（[L2-9](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L2-L9)）总结为「adding a method never touches this file」。

**④ finalize 子命令与 MTP 层的坑**——[tools/quant/jointfix/jointfix/cli.py:47-59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L47-L59)、[L76-85](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L76-L85)

finalize 有自己的 `--skip-shared-experts`，其 help 直白警告「必须与 quantize 时的同名 flag 一致」。`_deploy_skip_patterns` 的 docstring 解释了原因：校准循环只跑 `range(num_hidden_layers)`，MTP 层（回忆 u3-l5：层号 ≥ num_hidden_layers 的投机解码层）从未被访问、没有逐层产物，若不把 `mlp.shared_experts` 加进跳过名单，它们会被「RTN 兜底」意外量化——与整体意图相悖。这是「参数正交、行为耦合」时靠文档与约定对齐的典型案例。

**⑤ main 流程与一步到底**——[tools/quant/jointfix/jointfix/cli.py:100-132](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L100-L132)

`main` 按 cmd 分派；quantize 分支先实例化 backend（传入模型目录）与 method，`method.configure(args)` 应用方法参数，再用公共参数组装 `RunConfig` 交给 `calibrate_and_quantize`（core 主循环，u8-l2 精读）。随后 `_finalize_deploy` 默认启用（`enabled=not args.no_finalize`），把可部署模型**原地组装进 `--output`**，最后打印 `deployment model written to: ...`（README L120 所述的完成标志）。

**⑥ NPU 安装避坑**——[tools/quant/jointfix/README.md:57-63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/README.md#L57-L63)

原文给出完整因果链：torch/torch_npu 由 CANN 提供 → `pyproject.toml` 把 `torch>=2.1` 列为硬依赖 → CANN 的 torch 版本号不满足时 pip 会从 PyPI 拉 CPU 版 torch 盖掉 torch_npu →「废掉整台机器的 NPU 后端」。对策是 `pip install -e . --no-deps` 装本体、再单独补 `safetensors/transformers/numpy/pandas/pyarrow` 等非 torch 依赖。这一坑与 u8-l4 要部署的 w8a8 ansible 模板直接相关：量化在 NPU 容器里做，装错 torch 整个流程报废。

#### 4.3.4 代码实践

**实践目标**：在 CPU 环境安装 jointfix，观察 `--help` 的两种形态，理解方法参数注入。

1. `cd tools/quant/jointfix && pip install -e .`（CPU 机器可放心让 pip 装 torch）。
2. 执行 `jointfix quantize --help`，记下输出中**没有**哪些参数。
3. 执行 `jointfix quantize --method jointfix --help`，对比：这次应多出 `jointfix: joint (a,b) search` 参数组（`--objective`、`--write-quant`、`--num-iterations`、`--iter-ab-tol`、`--skip-shared-experts` 等）。
4. 思考并回答：为什么第 2 步看不到方法参数？（提示：看 cli.py L43-45 的 `_peek`——help 输出发生在 parse 之前，参数组是否注册取决于 `sys.argv` 里有没有 `--method`。）
5. 顺手执行 `jointfix finalize --help`，确认 finalize 的 `--skip-shared-experts` help 文本中的警告语。

**需要观察的现象**：两次 `quantize --help` 的差异只在方法参数组；核心参数（`--n-samples` 默认 128、`--seq-len` 默认 2048，见 [cli.py:31-32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L31-L32)）两次都在。注意 CLI 默认值与 README 生产推荐值（32/1024）不同——默认值偏保守，生产按 README 关键参数表调。

**预期结果**：以上命令输出**待本地验证**；若与预期不符，回到 `_peek` 与 `add_cli_args` 的源码逐行核对。

#### 4.3.5 小练习与答案

**练习 1**：`--no-finalize` 的使用场景是什么？

答案：想先确认逐层量化质量（检查 `layer_*.safetensors` 与误差日志）再组装；或分阶段跑批。之后用 `jointfix finalize --model <BF16目录> --quantized <中间产物目录> --output <部署目录>` 补组装。cli.py 的测试（test_cli.py L43-48）验证了该 flag 默认关闭、显式传入才开启。

**练习 2**：如果未来新增 `awq` 方法，cli.py 需要改吗？

答案：不需要。新方法只要继承 `QuantMethod`、实现 `add_cli_args`（注册自己的参数组）与 `process_layer`，打上 `@register_method("awq")` 并在 `methods/__init__.py` import，`--method awq` 及其专属参数即自动可用——这正是 cli.py 文件头注释「adding a method never touches this file」的含义。

**练习 3**：为什么 CPU 机器可以 `pip install -e .`，NPU 机器却强烈不建议？

答案：CPU 机器上 torch 本来就该来自 PyPI，硬依赖无害；NPU 机器上 torch_npu 与 CANN 强耦合，pip 拉来的 CPU 版 torch 会覆盖它（版本约束不满足时必然发生），导致 `torch.npu` 后端不可用。风险的不对称决定了安装策略的不对称。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——「安装 → 探查注册表 → 解剖 CLI → 画出调用关系图 → 用测试佐证」。

1. **安装与探查**（CPU 环境）：

   ```bash
   cd tools/quant/jointfix
   pip install -e .
   python -c "import jointfix; from jointfix.registry import available_backends, available_methods; print(available_backends(), available_methods())"
   ```

2. **解剖 CLI**：分别运行 `jointfix quantize --help`、`jointfix quantize --method jointfix --help`、`jointfix finalize --help`，把三份输出并排对比，标出哪些参数属于核心、哪些属于方法、哪些只在 finalize 存在。
3. **画调用关系图**：以一次 `jointfix quantize --backend pangu --method jointfix ...` 调用为对象，画出 `cli.main → registry.get_backend/get_method → backend/method 实例 → core.calibrate_and_quantize → （默认）_finalize_deploy` 的调用关系图，并在每条边上标注源码行号（cli.py L112-126、registry.py L33-42）。检查你的图是否体现「cli 只认识 registry 与 core，从不 import 具体模型实现」。
4. **测试佐证**：运行 `pytest tests/test_cli.py -v`（纯 CPU，不需要真权重，test_cli.py 开头注明该测试文件验证「quantize 默认一步组装、--no-finalize 可停在中间产物」）。观察 `test_quantize_finalize_is_default_on` 等用例如何用 `build_parser().parse_args([...])` 直接构造参数对象——这也是你日后给自己的扩展写测试的模板。
5. **回答收口问题**：为什么说「两条正交的轴 + 一个无关的 core」是比「一个大脚本」更好的架构？用「加一个新 backend 要改几个文件、加一个新 method 要改几个文件」量化回答。

**预期结果**：一张带行号的调用关系图 + 三份 help 的差异清单。所有命令输出**待本地验证**。

## 6. 本讲小结

- **W8A8** = 权重 8bit（per-output-channel、静态 scale 固化进文件）+ 激活 8bit（per-token、动态 scale 推理时现算）；jointfix 输入 BF16 模型，输出 vLLM 免 `--quantization` 直接加载的 compressed-tensors 模型，压缩约 1.9×。
- **三层正交架构**：backends/ 是模型轴（pangu 生产、hf 实验），methods/ 是方法轴（jointfix），与双方都无关的 core/ 负责校准主循环、统计、量化原语与部署组装；加模型与加方法互不影响。
- **registry 模式**：`@register_backend`/`@register_method` 装饰器在 import 时把类写入模块级字典，`--backend/--method` 字符串经 `get_backend/get_method` 查表成类，`available_*()` 实时喂给 argparse `choices`；注册的扳机在 `jointfix/__init__.py` 的两条 import。
- **CLI 两子命令**：`quantize` 默认一步到底（逐层量化后自动 finalize），`--no-finalize` 可停在 `layer_*.safetensors` 中间产物；`finalize` 单独组装并让 RTN 兜底未标定层。方法专属参数由方法自己注入 parser（`_peek` 两遍解析），cli.py 不随方法增长。
- **NPU 安装避坑**：`torch>=2.1` 硬依赖 + CANN 版 torch 版本号不满足 ⇒ pip 会拉 CPU 版 torch 覆盖 torch_npu；NPU 上必须 `pip install -e . --no-deps` 再单独补非 torch 依赖。
- finalize 的 `--skip-shared-experts` 必须与 quantize 一致，否则校准循环从未访问的 MTP 层共享专家会被 RTN 兜底意外量化。

## 7. 下一步学习建议

下一讲（u8-l2）进入 **core 模块**：`core/runner.py` 的逐层「前向收统计 → 量化 → 重前向验证」主循环、校准集加载（`calib_data.py`）、激活统计（`stats.py`）与 RTN/GPTQ 量化原语（`primitives.py`）。建议先自己通读 [tools/quant/jointfix/jointfix/core/runner.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/runner.py)，重点找 `RunConfig` 与 `calibrate_and_quantize` 的主循环骨架；学完 u8-l2 再去 u8-l3 看方法轴的 (a,b) 联合搜索算法，最后用 u8-l4 把量化产物真正部署成 INT8 服务。
