# 第 1 讲：项目定位与全景——openPangu-2.0-Infer 是什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 openPangu-2.0-Infer 这个仓库的定位：它是一个**部署仓**，核心内容是"在昇腾 NPU 上、通过 vLLM 插件体系部署 openPangu-2.0 系列 MoE 大模型"所需的脚本、模板与四个可独立编译的组件。
2. 区分两个模型规格：openPangu-2.0-Flash（92B）与 openPangu-2.0-Pro（505B），以及它们对应的配置目录和典型部署形态（如 1P1D、2P1D）。
3. 解释 PD 分离架构中 **P（Prefill）、D（Decode）、C（proxy）** 三类节点各自的职责，以及为什么要做 Prefill/Decode 分离。
4. 列出 `components/` 目录下四个子组件（omni-npu、omni-proxy、omni-cache、omni-eplb）各自解决什么问题，画出一张"组件职责地图"。

本讲是整本手册的第一讲，不要求你写过推理引擎代码，只需要对"大模型"和"Linux 服务器"有基本印象。

## 2. 前置知识

用最通俗的语言把本讲需要的几个概念先过一遍：

- **大模型推理（inference）**：把训练好的模型权重加载到加速卡（NPU/GPU）上，接收用户输入（prompt），逐个生成输出 token 的过程。推理服务通常以 HTTP API 形式对外提供（本仓库提供 OpenAI 兼容的 `/v1/chat/completions` 接口）。
- **Prefill 与 Decode**：推理分两个阶段。**Prefill（预填充）** 阶段一次性"读完"整个 prompt，为每个输入 token 计算 KV Cache（可以理解为"模型对这段话的记忆"）；**Decode（解码）** 阶段则一次生成一个 token，每一步都要用到 Prefill 阶段留下的 KV Cache。两个阶段的计算特性差异很大，这是本讲 PD 分离概念的根基。
- **NPU / 昇腾（Ascend）**：华为的神经网络处理器。本仓库面向的是 Ascend910C（文档中简称 A3）等昇腾推理卡，软件栈是 CANN + torch_npu，而不是 CUDA。
- **vLLM**：一个流行的开源推理框架。本仓库的思路是**不改 vLLM 源码**，而是以"插件（plugin）"的方式让 vLLM 跑在昇腾 NPU 上——这是 `components/omni-npu` 组件的核心工作。
- **MoE（Mixture of Experts，混合专家）**：一种模型结构：每个 token 只激活一小部分"专家"网络参与计算，从而在总参数量很大（如 505B）时保持较低的单 token 计算量。openPangu-2.0 系列是 MoE 模型，`components/omni-eplb` 解决的就是 MoE 的专家负载不均问题。
- **Docker 与 ansible**：Docker 提供容器化运行环境（推理服务都跑在容器里）；ansible 是一个批量运维工具，本仓库用它在一台"执行机"上统一拉起多台服务器上的容器与服务，命令形态是 `ansible-playbook -i inventory.yml template.yml --tags xxx`。

不需要现在就深入以上任何一项，后面的单元会逐个展开。

## 3. 本讲源码地图

本讲以两份顶层 README 为精读对象，辅助阅读一份 ansible 模板与四个组件的自我介绍：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 顶层中文部署手册：模型规格表、依赖版本、ansible 拉起 PD 分离服务的全流程 |
| [README_EN.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_EN.md) | 顶层英文手册，内容与中文版对应，可用来交叉确认术语 |
| [README_INT8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md) | INT8（W8A8 量化）权重版本的部署手册，补充了更多部署拓扑 |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) | 92B BF16 的 1P1D 服务模板，本讲只看其中 P/D/C 三段命令，感受"三类节点各跑什么" |
| [components/omni-npu/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md) | vLLM NPU 插件的自述 |
| [components/omni-cache/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md) | PD 分离 KV Cache 管理插件的自述 |
| [components/omni-eplb/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md) | MoE 专家动态编排 SDK（OmniPlacement）的自述 |
| [components/omni-proxy/omni_proxy/README_CN.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md) | Omni Proxy 请求调度引擎的自述 |

另外先记住仓库顶层的四个目录，下一讲会详细展开：

```text
openPangu-2.0-Infer/
├── README.md / README_EN.md / README_INT8*.md   # 部署手册
├── build/        # 仓库级构建入口（build.sh）
├── components/   # 四个可独立编译的组件：omni-npu / omni-proxy / omni-cache / omni-eplb
└── tools/        # 部署工具链：ansible（部署模板）/ docker（镜像构建）/ quant（量化）/ scripts（脚本）
```

注意：`components/` 下的四个目录都直接被本仓库的 git 跟踪（仓库里没有 `.gitmodules`），它们是"部署仓 + 四个自带源码的组件"的关系，而不是四个外部子仓库。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 项目背景**、**4.2 PD 分离基本概念**、**4.3 组件职责地图**。

### 4.1 项目背景：openPangu-2.0-Infer 是一个"部署仓"

#### 4.1.1 概念说明

打开仓库第一眼看到的不是某个程序的 `main()`，而是部署文档。这类仓库叫**部署仓**：它的"产品"是一套可复现的部署方法——ansible 编排、docker 镜像、启动模板，以及让 vLLM 在昇腾 NPU 上跑起来的插件组件。

为什么需要这样一个仓？因为把一个 92B/505B 的 MoE 模型部署成生产级推理服务，涉及的不只是"模型代码"：

- 模型怎么在 NPU 上跑 → 需要 vLLM + NPU 插件（omni-npu）；
- 多台机器怎么分工协作 → 需要 PD 分离与 KV 传输配置；
- 请求怎么分发 → 需要代理调度（omni-proxy）；
- 显存/内存不够怎么办 → 需要 KV Cache 卸载（omni-cache）、INT8 量化；
- MoE 专家负载不均怎么办 → 需要专家重排（omni-eplb）。

这个仓库把以上所有环节的**脚本与组件源码**收拢在一起，让你用几条 `ansible-playbook` 命令就能拉起一套完整服务。

#### 4.1.2 核心流程

从"拿到仓库"到"有一个能发请求的服务"，README 给出的主干流程是：

1. **确认硬件与规格**：以 Ascend910C（A3）为例，92B BF16 权重用 1P1D（两台 A3）拉起。
2. **拉取镜像**：`docker pull` 一个预装了全部依赖的 omniinfer 镜像。
3. **配置 ssh 免密**：P、D 节点之间要能互相免密登录。
4. **修改脚本**：在 inventory 里填 P/D/C 节点 IP，在模板 `environment` 里填日志路径、权重路径、镜像名等。
5. **启动容器**：`--tags run_docker` 在每台机器上创建容器。
6. **拉起服务**：`--tags run_server,run_proxy` 在容器里启动推理服务与代理。
7. **发请求验证**：向 proxy 端口（默认 7000）发一个 OpenAI 兼容请求。

其中第 5、6 步的完整实操是第 4 讲（u1-l4）的内容，本讲只需要建立整体印象。

#### 4.1.3 源码精读

**（1）开篇第一句就定义了最小部署形态。** README 说明以 Ascend910C (A3) 为例，openPangu-2.0-Flash 的 BF16 权重可通过 1P1D 拉起：一台 A3 组一个 P 节点、一台 A3 组一个 D 节点，共两台机器；多机部署用 ansible-playbook 统一拉起：

- [README.md:L3-L5](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L3-L5) —— 给出 1P1D = 2 台 A3 的最小规模，并说明执行机需要安装 ansible。

**（2）模型规格表：本仓库支持的两个"产品"。**

- [README.md:L9-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L9-L12) —— 这张表是全仓库最重要的一张表：

| 模型规格 | 模型名称 | 配置文件目录 | 典型部署 |
|---------|---------|-------------|------------|
| 92B | openPangu-2.0-Flash | `tools/ansible/92B/` | 1P1D（2 机 A3） |
| 505B | openPangu-2.0-Pro | `tools/ansible/505B/` | 2P1D（8 机 A3） |

也就是说：**92B 对应 Flash（轻量、易部署），505B 对应 Pro（大规模）**；ansible 配置按模型规格分成两个目录。英文版有同样的表格可交叉对照：[README_EN.md:L8-L11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_EN.md#L8-L11)。

INT8 手册还补充了更多拓扑（如 92B 的 3P1D、505B 的 4P81D16）：

- [README_INT8.md:L8-L11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L8-L11) —— INT8 权重版本的规格表，能看到同一模型支持多种 P/D 比例的拓扑。

命名法解读：`1P1D` = 1 个 Prefill 节点 + 1 个 Decode 节点；`3P1D` = 3 个 Prefill 节点 + 1 个 Decode 节点。P 多 D 少通常意味着业务里长 prompt（重 Prefill）占比较高。

**（3）依赖清单：镜像里已经装好了什么。**

- [README.md:L36-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L36-L47) —— 列出推理代码依赖的包与版本（镜像内已预装）。几个关键项：
  - `omni-npu 0.2.0` —— 就是 `components/omni-npu` 组件安装后的包名，**仓库组件以 Python 包的形式进入运行环境**；
  - `vllm 0.14.0+empty` —— 注意 `+empty` 后缀，说明镜像里的 vLLM 是一个"空壳"版本，真正的 NPU 适配由 omni-npu 插件在运行时补齐（第 2 单元会深入）；
  - `torch 2.9.0` / `torch-npu 2.9.0.post3...` —— PyTorch 与它的昇腾后端；
  - `transformers 4.57.6`、`tiktoken`、`tokenizers` —— 分词器与模型结构相关依赖。

#### 4.1.4 代码实践

**实践 A：中英对照读 README，提取"事实清单"。**

1. **实践目标**：熟悉两份手册的结构，验证自己能从文档中提取部署所需的关键事实。
2. **操作步骤**：
   1. 通读 [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) 全文（约 180 行），再快速扫一遍 [README_EN.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_EN.md)。
   2. 在笔记本上回答五个问题：① 支持哪两个模型？② 各自的配置目录在哪？③ 镜像从哪里拉？④ 有哪几个 ansible tags？⑤ 测试请求发到哪个端口、`model` 字段填什么？
3. **需要观察的现象**：两份 README 的章节是一一对应的（Pull Image / Configure SSH / Modify Scripts / Start Image / Launch Inference Service / Send Test Request）。
4. **预期结果**：五个问题都能在文中直接找到答案（分别对应 L9-L12 规格表、L19 镜像命令、L108/L133 的 `run_docker` 与 `run_server,run_proxy`、L154-L156 的 7000 端口与 `SERVED_MODEL_NAME` 说明）。

**实践 B：盘点依赖版本（可在任意有 Python 的机器上做）。**

1. **实践目标**：体会"omni-npu 是一个 Python 包"这一点。
2. **操作步骤**：在自己机器上查看本地是否装了 README 列出的包：`pip list | grep -iE "vllm|torch|tokenizers"`。
3. **需要观察的现象**：普通机器上通常没有 `torch-npu`、`omni-npu`——它们只在推理镜像/昇腾环境里存在。
4. **预期结果**：得出结论"本地开发机不是运行环境，运行环境是 README 里拉取的 omniinfer 镜像"；若想验证镜像内的版本，需要真实部署环境（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：`vllm 0.14.0+empty` 中的 `+empty` 大概率意味着什么？

<details><summary>参考答案</summary>

它表示镜像里安装的是一个"空实现"的 vLLM 构建：vLLM 的对外 API 仍在，但设备相关的默认实现被剥离/占位，真正的 NPU 适配由 `omni-npu` 插件在运行时通过 vLLM 的插件机制注入（omni-npu README 明确说明"Loaded via vLLM plugin entry points, no code changes to vLLM required"）。第 2 单元将验证这一点。

</details>

**练习 2**：为什么 ansible 配置要按 `92B/`、`505B/` 分成两个目录，而不是按硬件型号分？

<details><summary>参考答案</summary>

因为同一份模型在不同权重精度（BF16/INT8）与不同 P/D 拓扑（1P1D/3P1D/2P1D/4P81D16）下需要不同的启动参数组合，而模型规格（92B 还是 505B）决定了权重规模、并行度与节点数量级，是模板差异的第一来源；硬件型号（A3/A2）在 README_INT8 中是模板文件名里的次要后缀。

</details>

**练习 3**：你的业务长 prompt 很多、输出很短，应该倾向更多 P 还是更多 D？

<details><summary>参考答案</summary>

倾向更多 P（如 3P1D、4P1D）。长 prompt 意味着 Prefill 计算量大，是瓶颈；输出短则 Decode 压力小。P:D 的比例正是用来匹配业务的 Prefill/Decode 负载比的。

</details>

### 4.2 PD 分离基本概念：P、D、C 三类节点如何协作

#### 4.2.1 概念说明

一次聊天请求的推理分为两个阶段，它们的资源画像截然不同：

- **Prefill（预填充）**：把 prompt 的 \( L \) 个 token 一次性送入模型，注意力要两两计算，计算量约 \( O(L^2 d) \)，是**计算密集型**；好处是可以充分并行。
- **Decode（解码）**：每步只新算 1 个 token，但每步都要读取此前所有 token 的 KV Cache，单步计算量 \( O(L d) \) 而读取量也是 \( O(L d) \)，是**访存密集型**；难以利用算力，卡往往"喂不饱"。

**PD 分离（Prefill/Decode disaggregation）** 就是把两个阶段放到不同节点上分别部署：

- **P 节点（Prefill 节点）**：专门做预填充。收到请求后算完 prompt 的 KV Cache，然后把 KV Cache **通过网络传给** D 节点。
- **D 节点（Decode 节点）**：专门做解码。拿到 KV Cache 后逐 token 生成输出。
- **C 节点（proxy 节点）**：面向客户端的入口。README 明确写作 "C（proxy）节点"，部署上通常直接设在某个 P 节点的 IP 上。它运行 nginx + 调度代理，把并发请求按策略分配给后端的 P、D 服务。

这样分离的价值：

1. **各自选型**：P 用算力强的配置、D 用显存/带宽好的配置，互不牵制。
2. **消除相互干扰**：不分离时，一个超长 prompt 的 Prefill 会阻塞同卡上其他请求的 Decode，导致输出卡顿（tail latency 变差）。
3. **独立扩缩容**：长 prompt 业务多加 P，生成型业务多加 D（对应 1P1D/3P1D/4P81D16 等拓扑）。

代价是需要在 P、D 之间搬运 KV Cache，这正是 `KV_CONNECTOR`（KV 传输连接器）存在的意义，也是第 4 单元整单元的主题。

#### 4.2.2 核心流程

一次请求的完整流转（伪代码）：

```text
客户端 curl http://C节点:7000/v1/chat/completions
        │
        ▼
C 节点：nginx + Omni Proxy 调度模块（组件 omni-proxy）
        │  按调度策略选定一组上游：一个 P 服务 + 一个 D 服务
        ├──────────────┐
        ▼              ▼
P 节点服务        D 节点服务
(kv_producer)     (kv_consumer)
  计算 prompt KV     （先等 KV 到位）
        │              ▲
        └── KV Cache ──┘
            通过 KV_CONNECTOR（LLMDataDistConnector）传输
        │
        ▼
D 节点逐 token 生成 → 结果回流 C 节点 → 返回客户端
```

要点：

1. 客户端**只认识 C 节点**的 IP 和端口（默认 7000），完全感知不到后面有几台 P、几台 D。
2. P 与 D 是同一个模型的两份进程，靠启动参数区分角色：`--role prefill/decode` 与 `--kv-role kv_producer/kv_consumer`。
3. KV 传输由 `KV_CONNECTOR` 环境变量指定的连接器完成，默认值是 `LLMDataDistConnector`。

#### 4.2.3 源码精读

**（1）README 定义了三类节点的填法。**

- [README.md:L53-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L53-L57) —— 说明部署脚本在 `tools/ansible/92B` 与 `tools/ansible/505B`，1P1D 对应 inventory 与 BF16 模板两个文件；并明确"在 inventory 中填写 **P 节点**、**D 节点** 和 **C（proxy）节点** 的机器 IP，**proxy 节点** 设为 P 节点 IP"。英文版对应 [README_EN.md:L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_EN.md#L56)。

**（2）inventory 里 P 节点长什么样。**

- [README.md:L59-L71](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L59-L71) —— 这是 README 内嵌的 inventory 片段：P 组下每个 host 有 `node_rank`、`kv_rank`（KV 传输组的编号）、`node_port` / `api_port`（按 `global_port_base + port_offset.P + kv_rank` 计算的端口）、`host_ip`、`ascend_rt_visible_devices`（本机可见的 NPU 卡号列表）。本讲只需认识这些字段名，逐字段解读在第 3 讲（u1-l3）。

**（3）模板 environment 中的 PD 分离关键变量。**

- [README.md:L79-L98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L79-L98) —— `environment` 是模板里最需要修改的部分。与 PD 分离直接相关的两项：
  - `KV_CONNECTOR: "LLMDataDistConnector"`（[L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L86)）——指定 P、D 之间搬运 KV Cache 的连接器实现；
  - `SERVED_MODEL_NAME: "openPangu-2.0-Flash"`（[L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L87)）——对外服务名，**请求体里的 `model` 字段必须与它一致**。
  - 另有三个容器名变量 `DOCKER_NAME_P/D/C`（[L91-L93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L91-L93)），从命名就能看出 P、D、C 三类节点各自要起一个容器。

**（4）P 与 D 的启动命令分居两段，角色用参数区分。**

- [README.md:L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L100) —— "P 节点配置在 `run_vllm_server_prefill_cmd:`，D 节点配置在 `run_vllm_server_decode_cmd:`"。
- 打开 1P1D 模板可以看到真实实现：
  - [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L62) —— P 节点命令块 `run_vllm_server_prefill_cmd` 的起始行；块内 [L102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L102) 设置 `kv_role="kv_producer"`，[L128-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L128-L148) 是最终拼出的服务启动参数，其中 [L131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L131) `--role "prefill"`、[L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L132) `--kv-role ${kv_role}`、[L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L141) `--kv-connector ${KV_CONNECTOR}`。
  - [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L150) —— D 节点命令块 `run_vllm_server_decode_cmd` 的起始行，块内 [L229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L229) 直接写死 `--kv-role "kv_consumer"`。

  一句话总结：**P 生产 KV，D 消费 KV，`--role` 决定执行哪个阶段，`--kv-role` 决定 KV 传输方向。**

**（5）C 节点跑的是 nginx + proxy。**

- [README.md:L127-L139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L127-L139) —— `--tags run_server,run_proxy` 的说明中写道："C 节点会在容器内启动 nginx+proxy，在 master node 上启动 nginx 将并发的请求分配到各个节点上"。英文版对应 [README_EN.md:L126-L138](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_EN.md#L126-L138)。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L246-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L246-L291) —— `run_proxy_cmd` 的真身：先从 inventory 变量里展开 `PREFILL_API_SERVER_LIST` 与 `DECODE_API_SERVER_LIST_ALL` 得到上游地址，然后 [L275-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L275-L291) 进入 `components/omni-proxy/omni_proxy/` 目录执行 `bash omni_proxy.sh`，传入 `--listen-port`（proxy 监听端口）、`--prefill-endpoints` / `--decode-endpoints`（P、D 上游列表）、`--omni-proxy-pd-policy sequential`（PD 调度策略）等参数。**这就是"C 节点由 omni-proxy 组件负责"的直接证据。**

**（6）测试请求打到 C 节点。**

- [README.md:L152-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176) —— "服务启动后，向 proxy 节点端口（脚本默认为 7000）发送测试请求"，并给出完整 curl 示例；[L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L156) 强调请求体 `model` 字段必须与 `SERVED_MODEL_NAME` 一致。

#### 4.2.4 代码实践

**实践：从源码里"抄"出一次请求的流转路径。**

1. **实践目标**：不看任何二手描述，仅凭仓库文件确认"请求从客户端到 P、D 再返回"的每一跳，为综合实践的画图做素材准备。
2. **操作步骤**：
   1. 读 [README.md:L152-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176)，记下入口协议（HTTP POST）、路径（`/v1/chat/completions`）、目标（C 节点 IP:7000）。
   2. 打开模板 [L246-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L246-L291)，抄下 `--prefill-endpoints` / `--decode-endpoints` 这两个参数名，理解 proxy 持有全部 P、D 上游地址。
   3. 打开模板 [L128-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L128-L148) 与 [L229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L229)，记下 P、D 两侧区分角色的参数（`--role`、`--kv-role`、`--kv-connector`）。
   4. 用一句话+箭头串起全程，写在笔记里（参考 4.2.2 的伪代码图，但要用你自己的话）。
3. **需要观察的现象**：纯源码阅读即可完成，不需要运行环境。
4. **预期结果**：你能不假思索地回答"7000 端口是谁在听？请求如何知道该去哪台 P？KV 怎么到的 D？"三个问题。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 Prefill 是计算密集型而 Decode 是访存密集型？

<details><summary>参考答案</summary>

Prefill 一次并行处理 \( L \) 个 token，注意力要做 token 两两配对，浮点计算量约 \( O(L^2) \) 增长，算力是瓶颈；Decode 每步只算 1 个新 token，浮点量很少，但每步都要从显存读出此前 \( L \) 个 token 的全部 KV Cache，显存带宽是瓶颈。两者瓶颈不同，混布时会互相争抢和干扰。

</details>

**练习 2**：PD 分离后引入的新问题是什么？仓库里用哪个机制解决？

<details><summary>参考答案</summary>

新问题是 KV Cache 需要跨节点传输：P 算完后必须把 KV 搬到 D，搬得慢会抵消分离的收益。仓库用 `KV_CONNECTOR`（默认 `LLMDataDistConnector`）承担传输，相关的 `kv_role`（producer/consumer）、`kv_rank`、`kv-parallel-size` 等参数都是为这条传输链路服务的；第 4 单元精读其源码。

</details>

**练习 3**：如果把请求直接发给 P 节点的 api_port 而不是 C 节点的 7000 端口，猜猜会发生什么？

<details><summary>参考答案</summary>

P 节点上同样有 API server 在监听，请求大概率能被接受并完成 prefill，但缺少 proxy 的调度（不知道该把 KV 送给哪个 D、也无法做负载均衡与流控），服务行为不完整。README 规定的用法是发往 proxy 端口。此判断基于文档与模板结构，具体行为**待本地验证**。

</details>

### 4.3 组件职责地图：components/ 下的四个子模块

#### 4.3.1 概念说明

`components/` 下有四个目录，每个都是一个可独立编译/安装的组件，各自解决推理系统里的一个问题。先给一张总表（本讲只需记住"一句话职责"，细节由后续单元展开）：

| 组件 | 一句话职责 | 覆盖的痛点 |
|------|-----------|-----------|
| **omni-npu** | vLLM 的 NPU 平台插件，让 vLLM 不改源码跑在昇腾上 | 设备适配 |
| **omni-proxy** | 基于 Nginx 的请求调度引擎（C 节点上跑的就是它） | 请求分发与负载感知调度 |
| **omni-cache** | PD 分离 KV Cache 管理插件：KV 先卸载到主机内存再传输 | KV 传输与显存容量 |
| **omni-eplb** | OmniPlacement SDK：MoE 专家动态编排 | MoE 专家负载不均 |

四个组件不是"平级可选件"的关系：**omni-npu 是地基**（没有它 vLLM 根本跑不到 NPU 上），omni-proxy 是入口（每个部署都有 C 节点），omni-cache 与 omni-eplb 是按需启用的加速特性（ansible 模板文件名里带 `omni_cache` 后缀的就是启用了前者的部署）。

#### 4.3.2 核心流程

把四个组件放回 4.2 的请求流转图上，得到本讲的核心全景图：

```text
客户端 ──HTTP──▶ C 节点 [omni-proxy：nginx + 调度模块]
                   │ 分配上游
        ┌──────────┴──────────┐
        ▼                     ▼
   P 节点 [omni-npu]      D 节点 [omni-npu]
   prefill + kv_producer   decode + kv_consumer
        │                     ▲
        └──── KV Cache 传输 ──┘
             [默认：LLMDataDistConnector（omni-npu 内实现）]
             [可选：omni-cache —— KV 先卸载到主机内存再走网络]

   全局后台 [omni-eplb]：周期性采集 MoE 专家激活，动态重排专家放置
```

对应关系小结：

- **入口分发** → omni-proxy（模板 `run_proxy_cmd` 直接调用其 `omni_proxy.sh`）；
- **P/D 上的推理引擎** → vLLM + omni-npu 插件（镜像里的 `omni-npu 0.2.0` 包）；
- **KV 传输** → omni-npu 内的 connector 体系，可替换/叠加 omni-cache；
- **MoE 均衡** → omni-eplb，作用于 P/D 节点内部的专家权重分布。

#### 4.3.3 源码精读

**（1）omni-npu：vLLM 的"out-of-tree"平台插件。**

- [components/omni-npu/README.md:L1-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L1-L7) —— 自述为 "A vLLM (0.14.0) out-of-tree platform plugin that enables running vLLM on NPU (Ascend/torch_npu)"，三个要点：通过 vLLM 插件 entry points 加载（**对 vLLM 零代码修改**）；提供最小化的 NPU Platform、Worker 与独立的 NPU ModelRunner 适配；复用 vLLM 现有 serving API 不变。这正是理解整个仓库技术路线的钥匙。

**（2）omni-cache：KV Cache 的"中转仓库"。**

- [components/omni-cache/README.md:L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md#L7) —— 定位一句话："面向 vLLM 的 PD 分离 KV Cache 管理插件"。
- [components/omni-cache/README.md:L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md#L15) —— 机制展开："它在 Prefill 节点与 Decode 节点之间建立高效的 KV Cache 传输通道：Prefill 完成后，KV Cache 从 HBM 卸载到主机内存并通过 OX 发送；Decode 接收后从主机内存加载到 HBM 完成推理。"（HBM 是 NPU 显存；先卸载到主机内存可以让 P 节点尽快腾出显存接下一个请求。）

**（3）omni-eplb：MoE 专家的"调度员"。**

- [components/omni-eplb/README.md:L3-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md#L3-L7) —— 自述为面向 NPU 环境 MoE 系统的动态专家放置 SDK，核心特性包括专家重排（Expert Rearrangement）、层间不均匀专家放置、近实时重排与近实时激活采集。解决的问题是：MoE 各专家被选中的频率天然不均，热门专家所在的卡会先成为瓶颈，需要动态调整"专家住在哪张卡上"。

**（4）omni-proxy：第二代理由请求调度引擎。**

- [components/omni-proxy/omni_proxy/README_CN.md:L1-L8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L1-L8) —— 自述为"大模型高性能推理调度引擎"、"Omni Infer 开源项目的第二代请求调度引擎，基于 Nginx 构建并深度融合大模型推理特性"，通过性能监控、智能调度和缓存优化等手段提升推理性能。它就是 4.2 中 C 节点所运行的东西。

**（5）组件如何进入部署？** 回看 4.2.3（5）的模板片段：`run_proxy_cmd` 里 `cd /workspace/omniinfer/components/omni-proxy/omni_proxy/` 后执行 `omni_proxy.sh`（[模板 L275-L276](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L275-L276)）——组件源码会被带入容器并以脚本/Python 包的形式被调用；而 omni-npu 则是作为 `omni-npu 0.2.0` 包预装进镜像（[README.md:L36-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L36-L47)）。两条进入路径不同，但都不需要你手工编译——除非要做二次开发（那时用 `build/build.sh`，见第 2 讲）。

#### 4.3.4 代码实践

**实践：给四个组件做"实名登记"（源码阅读型实践）。**

1. **实践目标**：不看讲义结论，亲自从每个组件的 README 首屏摘出它的自我定位，填成一张表。
2. **操作步骤**：
   1. 依次打开四个文件的首屏：
      - [components/omni-npu/README.md:L1-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L1-L7)
      - [components/omni-cache/README.md:L1-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md#L1-L15)
      - [components/omni-eplb/README.md:L1-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/README.md#L1-L7)
      - [components/omni-proxy/omni_proxy/README_CN.md:L1-L8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L1-L8)
   2. 为每个组件抄一句"它自己说的话"作为职责描述，并标注出处行号。
   3. 把 4.3.1 的表格与你抄出的结果对照，找出任何不一致之处（如果发现讲义写错，以源码 README 为准——这也是本手册希望你养成的习惯）。
3. **需要观察的现象**：四个组件的 README 风格各异（英文/中文、面向开发者/面向运维），但首屏都直接回答"我是干什么的"。
4. **预期结果**：得到一张带出处行号的组件职责表，与 4.3.1 表格内容一致。

#### 4.3.5 小练习与答案

**练习 1**：四个组件中哪个是"每个部署都必须有"的，哪些是可选增强？

<details><summary>参考答案</summary>

omni-npu 必须有（vLLM 跑上 NPU 的前提，且镜像默认预装）；omni-proxy 在标准部署中也总是存在（C 节点由它构成，`run_proxy` 是默认 tag 之一）。omni-cache 与 omni-eplb 是按需启用的增强：前者对应模板文件名带 `omni_cache` 后缀的部署（如 `performance3P1D_92B_w8a8_open_omni_cache.yml`），后者需要单独配置启用。

</details>

**练习 2**：omni-cache 说 KV "从 HBM 卸载到主机内存"——这样做对 P 节点意味着什么？

<details><summary>参考答案</summary>

HBM（NPU 显存）是 P 节点最稀缺的资源，直接决定能同时处理多少请求。把已算好的 KV 尽快挪到更廉价、容量更大的主机内存，P 节点就能立刻腾出显存接纳下一个请求，提高吞吐；代价是引入一次额外的显存→内存拷贝。收益与代价的权衡在第 7 单元详细讨论。

</details>

**练习 3**：一个 MoE 模型某层有 3 个"热门专家"恰好都分布在同一张 NPU 卡上，会发生什么？哪个组件负责缓解？

<details><summary>参考答案</summary>

该卡成为该层前向的瓶颈（其他卡算完要等它），整体吞吐被拖慢。omni-eplb（OmniPlacement）通过近实时采集专家激活统计并动态重排专家放置来缓解，还支持层间不均匀分布与冗余专家提高可用性。

</details>

## 5. 综合实践

**任务：手工绘制"一次请求的完整流转示意图"，并标注每个环节由哪个组件负责。**（本讲规格指定的主实践）

1. **实践目标**：把本讲三个模块（项目背景、PD 分离、组件地图）的输出整合成一张图，作为你后续学习所有单元的"挂图"——以后每学一个组件，就回到这张图上把对应环节画细。
2. **操作步骤**：
   1. 准备一张纸或任意画图工具。横向画出：客户端 → C 节点 → P 节点 → （KV 传输）→ D 节点 → 原路返回。
   2. 在每个节点方框里标注两类信息：
      - **运行内容**：C 节点写"nginx + Omni Proxy"；P 节点写"`vllm serve --role prefill --kv-role kv_producer`"；D 节点写"`vllm serve --role decode --kv-role kv_consumer`"（依据：[模板 L131-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L131-L132)、[L229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L229)）；
      - **负责组件**：C 节点→omni-proxy；P/D 节点→vLLM + omni-npu；KV 箭头→KV_CONNECTOR（默认 LLMDataDistConnector），并用虚线画出可选的 omni-cache 主机内存中转路径；角落里画一个后台小框标注 omni-eplb。
   3. 在客户端箭头上标注协议与端口：`POST http://<C节点IP>:7000/v1/chat/completions`，请求体 `model` 必须等于 `SERVED_MODEL_NAME`（依据：[README.md:L152-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176)）。
   4. 把图中每个标注的出处（文件+行号）写在图旁边，形成"证据链"。
3. **需要观察的现象**：画完后自查三问——入口端口写了吗？P 与 D 的角色参数区分了吗？四个组件都出现且只出现在它们负责的环节上了吗？
4. **预期结果**：一张信息完整的示意图；与 4.3.2 的全景图结构一致、但由你独立完成并带出处标注。
5. **进阶（可选，需要真实环境）**：按第 4 讲的方法拉起一套 1P1D 服务，用 `curl` 发出 [README.md:L158-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L158-L176) 的请求，对照图从 C 节点的 nginx 日志（`nginx_access.log`，见 [模板 L280-L282](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L280-L282) 的日志路径参数）与 P/D 的 `server_0.log`（[README.md:L141-L144](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L141-L144)）里找到这次请求的痕迹。没有真实环境时此步跳过（**待本地验证**）。

## 6. 本讲小结

- openPangu-2.0-Infer 是一个**部署仓**：用 ansible + docker 把 openPangu-2.0-Flash（92B）/Pro（505B）两个 MoE 模型以 PD 分离形态部署到昇腾 NPU 上，核心组件以 vLLM 插件形式工作（`vllm 0.14.0+empty` + `omni-npu 0.2.0`）。
- **PD 分离**：P 节点做计算密集的 Prefill 并充当 `kv_producer`，D 节点做访存密集的 Decode 并充当 `kv_consumer`，KV Cache 经 `KV_CONNECTOR`（默认 `LLMDataDistConnector`）跨节点传输；分离的价值是按阶段独立扩缩容、消除长 prompt 对解码的干扰。
- **C 节点是统一入口**：容器内跑 nginx + proxy（omni-proxy 组件），客户端只面向它的 7000 端口发 OpenAI 兼容请求，`model` 字段必须与 `SERVED_MODEL_NAME` 一致。
- **四个组件各司其职**：omni-npu（vLLM NPU 平台插件，地基）、omni-proxy（请求调度引擎，入口）、omni-cache（KV 卸载到主机内存的 PD 传输增强）、omni-eplb（MoE 专家动态编排）。
- 读懂仓库的钥匙是两份顶层 README 与 `tools/ansible/<规格>/` 下的 inventory/模板：README 讲"做什么"，模板里的 `run_vllm_server_prefill_cmd` / `run_vllm_server_decode_cmd` / `run_proxy_cmd` 三段命令讲"具体怎么做"。

## 7. 下一步学习建议

- **下一讲（u1-l2：仓库目录结构与四大组件划分）**：深入 `build/build.sh` 与 `tools/` 工具链，弄清四个组件如何被分别编译、以及部署工具与运行组件的边界。
- **按顺序完成第 1 单元**：u1-l3 会逐字段解读 inventory（`node_rank`、`kv_rank`、`port_offset` 等），u1-l4 带你完整拉起 1P1D BF16 服务，u1-l5 讲请求验证与 HCCL 通信排障。
- **提前浏览（不求全懂）**：`components/omni-npu/README.md` 全文——它是第 2 单元"插件机制"的预习材料；以及 `tools/ansible/92B/` 目录下的文件列表，感受"同一模型、多种模板"的组织方式。
