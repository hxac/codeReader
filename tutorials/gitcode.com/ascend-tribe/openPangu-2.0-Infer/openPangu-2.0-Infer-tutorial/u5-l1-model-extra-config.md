# 模型最佳实践配置系统（ModelExtraConfig）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TaskConfig`、`ModelParallelConfig`、`ModelOperatorOptConfig` 三个配置类各自的分工，以及它们如何被聚合进 `ModelExtraConfig` 单例。
2. 完整复述一次配置自动加载的流程：`hf_config` 超参指纹 → `match_hf_configs.json` 反查模型名 → `best_practice_configs.json` 按「模型 + 硬件 + 精度 + PD 形态」四元组定位具体 json 文件。
3. 掌握两条覆盖/开关通道：`ADDITIONAL_CONFIG`（`--additional-config`）控制性能模式，`CUSTOM_MODEL_CONFIG_PATH` 直接钉死一份配置文件（优先级最高）。
4. 会看日志里的 `ModelExtraConfig:` 回显来确认「到底生效了哪份配置」。

本讲属于单元 5「性能与功能机制」的第一讲，承接 u2-l3 讲过的 `NPUWorker.init_device` 生命周期——配置加载正是发生在这个时机的第三步。

## 2. 前置知识

阅读本讲前，建议先理解以下几个概念（不熟悉可先回顾对应讲义）：

- **hf_config**：HuggingFace 模型目录下 `config.json` 解析后的对象，存放 `hidden_size`、`num_attention_heads` 等架构超参。omni-npu 用它做「模型指纹识别」。
- **PD 分离与 ROLE 环境变量**（u1-l1、u4-l1）：P 节点进程 `ROLE=prefill`，D 节点进程 `ROLE=decode`。同一套代码在 P、D 两侧行为不同，配置也因此分成 `_p.json` 与 `_d.json` 两份。
- **`NPUWorker.init_device`**（u2-l3）：vLLM 执行器最先调用的 worker 钩子，设备绑定与分布式组网之后、模型加载之前，会调用本讲的 `load_model_extra_config`。
- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器自动生成 `__init__` 等方法；`__post_init__` 是构造完成后立即执行的校验/联动钩子。
- **单例（singleton）**：模块级 `model_extra_config = ModelExtraConfig()` 在每个 worker 进程里只有一份，全仓库任何地方 `import` 到的都是同一个对象。
- **为什么需要这套系统**：omni-npu 里有上百个 NPU 专属优化开关（多流重叠、权重预取、MoE 通信策略、序列并行……）。vLLM 的命令行不可能为每个模型暴露这么多参数；把「什么模型 + 什么硬件 + 什么部署形态该开什么开关」沉淀成随插件发布的 json 文件，部署者零成本拿到最佳实践，这就是 ModelExtraConfig 解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py) | 核心加载器：三个配置类定义、模型识别、json 匹配、单例维护 |
| [components/omni-npu/src/omni_npu/model_config/config_loader/features.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/features.py) | 配置加载后的特性后处理（eager 模式裁剪、序列并行门禁） |
| [components/omni-npu/src/omni_npu/model_config/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md) | 官方使用说明：如何新增配置项与模型 json |
| [components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json) | 模型超参指纹登记表：模型名 → 一组架构超参 |
| [components/omni-npu/src/omni_npu/model_config/configs/high_throughout/best_practice_configs.json](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/high_throughout/best_practice_configs.json) | 高吞吐模式的路由表：四元组 → 具体 json 文件名 |
| [components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json) | 低时延模式的路由表（同上结构） |
| [components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_p.json) | openPangu 系列的实际配置文件（P/D 各一份，含多个变体） |
| [components/omni-npu/src/omni_npu/worker/npu_worker.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_worker.py) | 加载时机：`init_device` 中调用 `load_model_extra_config` |
| [components/omni-npu/tests/unit/models/test_loader.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/models/test_loader.py) | 无需 NPU 的加载器单测（本讲实践的重要参照） |

## 4. 核心概念与源码讲解

### 4.1 配置分层：三大配置类与 ModelExtraConfig 单例

#### 4.1.1 概念说明

「模型最佳实践配置」在代码里叫 **ModelExtraConfig**（模型附加配置），它把所有 NPU 侧开关分成三层，每层回答一个不同的问题：

| 配置类 | 回答的问题 | 典型字段 |
| --- | --- | --- |
| `TaskConfig` | 我在什么环境下跑？ | `model_name`、`hardware_platform`、`is_pd_disaggregation`、`is_prefill_node`、`quant_type`、`graph_mode`、`enable_low_latency` |
| `ModelParallelConfig` | 用什么并行策略？ | `ena_seq_parallel`、`ena_context_parallel`、`enable_flashcomm2`、`layer_parallel_config`、`ena_dp_lmhead_parallel` |
| `ModelOperatorOptConfig` | 哪些算子级优化要开？ | `moe_comm_strategy`、`enable_prefetch` 及各预取大小、`use_rope_fusion_op`、`use_noncontiguous_kv`、`shared_expert_multi_stream` |

分层的好处是**关注点分离**：Task 层是「事实描述」，由启动环境自动推导，json 文件一般不写它；后两层是「策略选择」，才是 json 文件真正装载的内容（对应 json 里的 `model_parallel_config` 与 `operator_optimization_config` 两个顶层键）。

三个类聚合成 `ModelExtraConfig`，并以**模块级单例** `model_extra_config` 存在——全仓库消费方式统一为：

```python
from omni_npu.model_config.config_loader.loader import model_extra_config
model_extra_config.operator_opt_config.xxxx   # README 推荐的调用姿势
```

#### 4.1.2 核心流程

一次完整的配置装载时序（发生在每个 worker 进程内部）：

```text
vLLM 执行器启动 worker
  └─ NPUWorker.init_device()                    # u2-l3 讲过的生命周期第一步
       ├─ 绑定 npu 设备 + 分布式组网（HCCL）
       └─ load_model_extra_config(model_config, vllm_config, scheduler_config)
            ├─ parse_hf_config(hf_config)        # ① 超参指纹 → (model_name, quant_type)
            ├─ 读环境变量 ROLE / PREFILL_POD_NUM / DECODE_POD_NUM
            ├─ 读 additional_config（--additional-config 传入的 JSON）
            ├─ 探测设备型号 → hardware_platform（A2/A3/A5）
            ├─ 推导 graph_mode（eager/acl_graph/ge_graph）
            ├─ update_task_config(...)           # ② 填充 TaskConfig
            │    └─ _init_model_extra_config()   # ③ 定位并加载 json（详见 4.2）
            │         ├─ CUSTOM_MODEL_CONFIG_PATH 分支（优先）
            │         └─ best_practice 自动匹配分支
            ├─ _validate_config()               # ④ features.py 后处理（详见 4.3）
            └─ _print_model_config()            # ⑤ 日志回显「ModelExtraConfig: {...}」
之后任何模块 import 单例即可读取
```

注意：D 节点上有 16 个 DP server 进程（u1-l4），**每个进程都各自执行一遍上述流程**，因此每个进程都有独立的一份 `model_extra_config`。

#### 4.1.3 源码精读

先看加载入口。`load_model_extra_config` 从框架侧三类 config 里「榨取」环境事实：

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L27-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L27-L48) —— 入口函数前半段：调用 `parse_hf_config` 得到模型名与量化类型；用 `ROLE` 环境变量判断是否 PD 分离、是否 prefill 节点；从 `additional_config` 读 `enable_low_latency` / `enable_pd_elastic_scaling` / `enable_omni_cache` 三个开关；根据 `enforce_eager` 与 `use_gegraph` 推导 `graph_mode` 三选一。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L53-L62](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L53-L62) —— 设备型号映射：`Ascend910B*` → `A2`，其余 `Ascend910*`（即 910C）→ `A3`，`Ascend950*` → `A5`，不认识的设备直接抛 `ValueError`。这个字符串就是后面匹配 `best_practice_configs.json` 中 `hardware` 字段的钥匙。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L64-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L64-L80) —— 把上述事实经 `update_task_config` 灌进 TaskConfig，随后执行 `_validate_config`（特性后处理）与 `_print_model_config`（日志回显）。`prefill_node_num` / `decode_node_num` 分别取自 `PREFILL_POD_NUM` / `DECODE_POD_NUM` 环境变量，缺省 1（这两个变量由 pd_run.sh 导出，见 u4-l4）。

接着看三个配置类的定义：

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L83-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L83-L97) —— `TaskConfig`：全部是环境事实字段。注意默认值只是占位（如 `model_name="deepseek_v3"`），真实值由 `update_task_config` 运行时覆盖。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L100-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L100-L112) —— `ModelParallelConfig`：并行策略开关。`ena_seq_parallel` 是「模型内全局序列并行」（TP 切 token），`ena_context_parallel` 是 DSA 专属的序列并行且依赖前者；`layer_parallel_config` 是个自由字典，按层名存放通信变换（如 `self_attn.o_proj` 的 x/y transform）。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L114-L184](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L114-L184) —— `ModelOperatorOptConfig`：字段最多的类。值得关注的默认值：`gmm_nz=True`（MoE 权重走 FRACTAL_NZ 布局，见 u3-l3）、`shared_expert_multi_stream=True`（共享专家默认走旁路流，与 u3-l3 呼应）、`moe_comm_strategy="dispatch_combine"`（MoE 通信默认走 HCCL 融合分发回收）、`enable_prefetch=True` 加一组按 MB 计的预取大小。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L186-L213](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L186-L213) —— `__post_init__` 校验联动：`enable_prefetch=False` 时把全部 `*_prefetch` 大小归零并打 warning；`enable_pipeline_comm` 与 `enable_round_pipeline_comm` 同时为 True 直接抛 `ValueError`；`unquant_bmm_nz=True` 时设置 `torch.npu.config.allow_internal_format = True`（允许 torch_npu 内部格式转换）。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L216-L228](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L216-L228) —— `ModelExtraConfig` 聚合三个配置类 + 模块级单例创建 + `filter_dict_by_dataclass` 工具函数：后者用 dataclass 的字段名集合过滤 dict，**json 里写了配置类上不存在的键会被静默丢弃**（单测有覆盖，见 [components/omni-npu/tests/unit/models/test_loader.py:L354-L368](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/models/test_loader.py#L354-L368)）。这既是容错也是陷阱：手写 json 时拼错字段名不会有任何报错，只是不生效。

加载时机在 worker 侧：

- [components/omni-npu/src/omni_npu/worker/npu_worker.py:L100-L117](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_worker.py#L100-L117) —— `init_device` 中先完成设备绑定与 HCCL 组网，然后调用 `load_model_extra_config`（第 117 行，注释 "Initialize the model best practice configs"）。位置很关键：它必须早于模型构建——模型类在 `__init__` 里就要读配置决定结构。

消费侧的两个真实例子：

- [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L148-L150](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L148-L150) —— MLP 层构造时读取 `moe_comm_strategy`，随后在 [L174-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L174-L192) 决定前向中用 `all_gather + all_reduce` 还是 `allreduce`（对应 u3-l3 讲过的 MoE 三种通信策略）。
- [components/omni-npu/src/omni_npu/layers/prefetch.py:L32-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/prefetch.py#L32-L41) —— 预取初始化第一行就检查 `enable_prefetch` 与 `attn_prefetch`，关闭时直接返回。

阅读时值得留意的三个源码细节（均为事实观察，非臆测）：

1. [features.py:L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/features.py#L21) 写入的 `enable_scmoe_multi_stream` 并不是 `ModelOperatorOptConfig` 的既有字段（当前字段名是 `shared_expert_multi_stream`，全仓库检索无消费点）——非 slots 的 dataclass 允许动态挂属性，所以这行不报错，但实际没有关掉任何东西。读代码时不要被它误导。
2. `use_mome_inplace_update` 在 [loader.py:L167](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L167) 与 [L179](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L179) 声明了两次，Python 类体后者覆盖前者，值相同所以无害。
3. `moe_seq_split_length` 的默认值在类体解析（import）时就读取 `ENABLE_OMNI_CACHE` 环境变量（[loader.py:L173-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L173-L176)），而不是构造时——若运行中途才设置该环境变量，不会影响这个默认值。

#### 4.1.4 代码实践

**实践目标**：不改任何文件，在推理容器里直接实例化 `ModelOperatorOptConfig`，验证 `__post_init__` 的联动规则（与单测 `test_model_operator_opt_config_post_init_enable_prefetch_false` 同构）。

**操作步骤**（在 u1-l4 部署好的 P 或 D 节点容器内执行；容器内已 `pip install -e` 装好 omni-npu）：

1. 进入容器：`docker exec -it <P或D容器名> bash`。
2. 执行以下命令（示例代码，非项目原有）：

```bash
python -c "
from omni_npu.model_config.config_loader.loader import (
    ModelOperatorOptConfig, model_extra_config)

# 1) 关闭预取，观察 __post_init__ 联动归零
c = ModelOperatorOptConfig(enable_prefetch=False)
print('enable_prefetch =', c.enable_prefetch)
print('expert_gate_up_prefetch =', c.expert_gate_up_prefetch)   # 预期 0
print('attn_prefetch =', c.attn_prefetch)                       # 预期 0

# 2) 互斥项校验
try:
    ModelOperatorOptConfig(enable_pipeline_comm=True,
                           enable_round_pipeline_comm=True)
except ValueError as e:
    print('ValueError:', str(e)[:60])

# 3) 看一眼单例当前的默认值
print('singleton moe_comm_strategy =',
      model_extra_config.operator_opt_config.moe_comm_strategy)
"
```

**需要观察的现象**：第 1 段打印出 `expert_gate_up_prefetch = 0`、`attn_prefetch = 0`，同时终端出现一条 warning "When enable_prefetch is false, prefetch_Mb must be set to 0."；第 2 段打印 `ValueError: Conflicting communication configuration...`；第 3 段打印 `dispatch_combine`。

**预期结果**：与 [components/omni-npu/tests/unit/models/test_loader.py:L122-L151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/models/test_loader.py#L122-L151) 的断言一致。此路径不触碰 NPU 设备，但 `loader.py` 顶层 `import torch_npu`，故必须在装有 CANN/torch_npu 的容器里运行；纯 x86 环境 import 即失败，此时可改跑该单测文件（它 mock 掉了 torch_npu）。容器内的具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `moe_comm_strategy` 放在 `ModelOperatorOptConfig`，而 `ena_seq_parallel` 放在 `ModelParallelConfig`？

**参考答案**：`moe_comm_strategy` 决定 MoE 层分发/回收 token 用哪种集合通信算子（dispatch_combine/all2allv/allgather_reducescatter/allreduce），是算子实现层面的选择；`ena_seq_parallel` 决定 TP 组内 activation 按序列维切分这一并行拓扑，影响所有层的通信编排。按「算子优化 / 并行策略」的分层标准，前者归 operator_opt，后者归 parall。

**练习 2**：json 配置文件里误把 `enable_prefetch` 拼成了 `enable_prefecth`，会发生什么？

**参考答案**：不会有任何报错。`filter_dict_by_dataclass` 只保留 dataclass 已有字段（[loader.py:L226-L228](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L226-L228)），拼错的键被静默丢弃，该配置项维持默认值（本例为 True，恰好还是开的）。排查方法是对照日志里 `ModelExtraConfig:` 的回显确认最终值。

**练习 3**：`TaskConfig` 为什么几乎从不出现在模型 json 文件里？

**参考答案**：TaskConfig 描述的是运行环境事实（模型名、硬件、是否 PD、图模式等），这些由 `load_model_extra_config` 在运行时从 `hf_config`、设备型号、环境变量和 vllm_config 自动推导；json 文件只负责「策略选择」（parall 与 operator_opt 两段），由 [loader.py:L330-L331](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L330-L331) 分别解析这两个键可以印证。

### 4.2 模型识别匹配：match_hf_configs.json 与 best_practice_configs.json

#### 4.2.1 概念说明

自动匹配要解决的问题是：**同一个 `model_type` 可能有多个规格**（如 `openpangu_v2` 同时对应 35B/92B/505B），而它们需要的最佳实践配置不同。vLLM 只把 `model_type` 告诉插件，规格信息必须自己反推。

方案是两级查表：

1. **第一级：超参指纹反查模型名**。`match_hf_configs.json` 为每个已知「模型名」登记一组架构超参；加载器拿真实 `hf_config` 逐条比对，全中者即命中。就像用身高体重鞋号在花名册里找人。
2. **第二级：四元组路由到文件**。`best_practice_configs.json` 按 `(model, hardware, precision)` 三元组列出条目，每个条目的 `configs` 字典再按 PD 形态（如 `1P1D`、`2P1D`、`hybrid`、`pd_elastic_scaling`）给出 P/D 各自该加载的 json 文件。

#### 4.2.2 核心流程

第一级匹配的判定条件可以写成：

\[ \text{match}(m) \iff \forall (k, v) \in \text{fingerprint}(m):\ \text{hf\_config}[k] = v \]

即登记表里**每一个**键值对都必须在 `hf_config` 中存在且相等（`null` 值要求该超参缺失或为 null）。然后：

```text
parse_hf_config(hf_config):
    matches = [m for m in match_hf_configs if 全部超参相等]
    if len(matches) == 0:  model_name = hf_config.model_type        # 兜底
    elif len(matches) == 1: model_name = matches[0]
    else:                    # 多命中：deepseek_v3/v32 有特判，其余抛 RuntimeError
    quant_type = 从 quantization_config 推导（w8a8c16 / hif8 / mxfp8 / bf16）

_get_best_practice_config(task_config):
    mode = 'low_latency' if enable_low_latency else 'high_throughout'
    条目 = 在 {mode}/best_practice_configs.json 中找 (model, hardware, precision) 三者全等的第一条
    pd_scheme =
        '1P1D' 这类 f'{P数}P{D数}D'   # PD 分离且未开弹性扩缩容
        'pd_elastic_scaling'           # PD 分离且开弹性扩缩容
        'hybrid'                       # 未设 ROLE（单机混部，非 PD）
    文件 = 条目.configs[pd_scheme] 里按 is_prefill_node 取 prefill/decode_config_file（hybrid 取 config_file）
    读该 json → {model_parallel_config, operator_optimization_config}
    任一步落空 → 打 warning，返回 None → 使用三个配置类的默认值
```

#### 4.2.3 源码精读

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L244-L276](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L244-L276) —— 第一级匹配主循环：`vars(hf_config)` 把对象转 dict，逐条目逐键比较；零命中时兜底用 `hf_config.model_type`；多命中时仅对 deepseek_v3/deepseek_v32 有硬编码仲裁，其余直接抛 `RuntimeError`。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L278-L312](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L278-L312) —— 量化类型推导：`format == "int-quantized"`（compressed-tensors 格式）时拼出 `w{权重位数}a{激活位数}`，再看 `kv_cache_scheme`：dict 则追加 `c{kv位数}`、字符串 `Opti-C8` 追加 `_fa_c8`、其余追加 `c16`；`quant_method` 为 `hifloat8`/`mxfp8` 分别得 `hif8`/`mxfp8`；没有量化信息则是 `bf16`。所以 jointfix 产出的 W8A8 权重（u8 单元）在这里被识别为 `w8a8c16`。

- [components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json:L247-L266](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json#L247-L266) —— openPangu-2.0 的两条指纹：`openpangu_v2_92B`（hidden_size 2560、48 头、192 专家、1 共享专家）与 `openpangu_v2_505B`（hidden_size 5120、64 头、384 专家）。它们的 `model_type` 都是 `openpangu_v2`——这正是必须靠超参指纹而非 model_type 区分规格的实例。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L358-L377](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L358-L377) —— 第二级路由：先按 `enable_low_latency` 选目录（默认 `high_throughout`，注意目录名拼写就是 `high_throughout`），再在条目列表中线性查找 model/hardware/precision 三者字符串全等的第一条；`pd_scheme` 的三种取值逻辑也在这段。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L379-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L379-L413) —— 文件选择与兜底：找不到三元组或找不到 pd_scheme 都只打 warning 并返回 `None`（随后使用默认配置）；找到则按 `is_prefill_node` 取 `prefill_config_file` / `decode_config_file`（hybrid 取 `config_file`），拼接完整路径；**文件不存在则直接抛 RuntimeError**（登记了却没放文件属于硬错误）。

- [components/omni-npu/src/omni_npu/model_config/configs/high_throughout/best_practice_configs.json:L222-L233](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/high_throughout/best_practice_configs.json#L222-L233) —— 高吞吐模式下的 openpangu_v2_92B 条目：A3 + bf16，`1P1D` 形态 P 侧加载 `openpangu_v2/openpangu_v2_92b_bf16_a3_1p1d_p.json`、D 侧加载 `_d.json`。模型名字段与 match_hf_configs 的键完全一致，自动匹配可以走通。

- [components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json:L124-L135](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json#L124-L135) —— 低时延模式下的 92B 条目，`1P1D` 指向 `openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_p.json` / `_d.json`。

**⚠ 源码观察（影响你对自动匹配的预期）**：low_latency 路由表里 openPangu 条目的 `model` 字段写的是 `pangu_v2_moe_92B` / `pangu_v2_moe_505B` / `pangu_v2_moe_34B`（如 [L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json#L65)、[L125](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json#L125)、[L185](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json#L185)），而第一级匹配输出的名字来自 match_hf_configs.json 的键（`openpangu_v2_92B` 等），两者对不上、且全仓库无改名逻辑。因此**低时延模式下 openPangu 的自动匹配会落入「打 warning + 默认配置」分支**。这解释了为什么生产 ansible 模板一律显式设置 `CUSTOM_MODEL_CONFIG_PATH` 钉死文件（见 4.3.3），而不是依赖自动匹配。你可以在日志中搜索 warning 文案 "was not found in best_practice_configs.json" 验证此行为（待本地验证）。

- [components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_p.json:L1-L21](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_p.json#L1-L21) —— 一份真实 P 侧配置：并行层开 `ena_seq_parallel + ena_context_parallel + enable_flashcomm2`；算子层选 `moe_comm_strategy: allgather_reducescatter`（prefill 侧聚合通信更划算，对照 u3-l3 的策略选择）并开 rope 融合、非连续 KV 等。

- [components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_d.json:L1-L19](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_d.json#L1-L19) —— 对应 D 侧配置：不开序列并行，`moe_comm_strategy` 换回 `dispatch_combine`。同一模型 P/D 两侧行为差异就体现在这两份文件的对比上。

官方文档对「新增一个模型 json」的完整流程描述在 [components/omni-npu/src/omni_npu/model_config/README.md:L24-L101](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L24-L101)：先在 match_hf_configs.json 登记，再确定使用场景（高吞吐/低时延/自定义），然后在 best_practice_configs.json 挂路由，最后把文件放进对应模型目录；并说明 UT 中有「配置类对象唯一性」校验防冗余——对应集成测试 [components/omni-npu/tests/integration/models/test_loader_integration.py:L197-L264](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/integration/models/test_loader_integration.py#L197-L264)（同目录 `_p/_d` 后缀的同名变体被豁免）。

#### 4.2.4 代码实践

**实践目标**：用伪造的 `hf_config`（不加载真权重、不触碰设备）验证第一级指纹匹配，并手工推演第二级路由，加深对两级查表的理解。

**操作步骤**（容器内执行；示例代码，非项目原有）：

1. 构造与 `match_hf_configs.json` 中 `openpangu_v2_92B` 完全一致的超参，调用 `parse_hf_config`：

```bash
python -c "
from types import SimpleNamespace
from omni_npu.model_config.config_loader.loader import parse_hf_config

# 超参抄自 match_hf_configs.json 的 openpangu_v2_92B 条目
hf = SimpleNamespace(
    model_type='openpangu_v2', hidden_size=2560,
    num_attention_heads=48, vocab_size=151552,
    intermediate_size=9216, n_routed_experts=256,
    n_shared_experts=1, moe_intermediate_size=1024)
print(parse_hf_config(hf))     # 预期 ('openpangu_v2_92B', 'bf16')
"
```

2. 把 `hidden_size` 改成 4096 再跑一次，观察输出。
3. 纯阅读推演（无需运行）：对照 [high_throughout/best_practice_configs.json:L222-L243](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/high_throughout/best_practice_configs.json#L222-L243) 回答：`TaskConfig(model_name='openpangu_v2_92B', hardware_platform='A3', quant_type='bf16', is_pd_disaggregation=True, is_prefill_node=True, prefill_node_num=1, decode_node_num=1, enable_low_latency=False)` 会加载哪个文件？`enable_low_latency=True` 时又会怎样？

**需要观察的现象**：步骤 1 打印 `('openpangu_v2_92B', 'bf16')`；步骤 2 打印的模型名退化为 `openpangu_v2`（零命中兜底）。

**预期结果**：步骤 3 的推演结论——`enable_low_latency=False` 时加载 `high_throughout/openpangu_v2/openpangu_v2_92b_bf16_a3_1p1d_p.json`；`enable_low_latency=True` 时因 4.2.3所述的命名不一致而落入 warning + 默认配置分支。步骤 1/2 与单测 [test_loader.py:L162-L183](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/models/test_loader.py#L162-L183) 同构，容器内「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`match_hf_configs.json` 里 `longcat-flash` 的 `model_type` 是 `null`（[L154-L164](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/match_hf_configs.json#L154-L164)）。它怎样才能命中？

**参考答案**：匹配条件是「每个键的值相等」。`hf_config.model_type` 若缺失，`vars()` 中就没有 `model_type` 这个键，`key not in vars_hf_config` 成立 → 不相等 → 该条目不命中；只有当 hf_config 里显式存在 `model_type=null`（或值就是 None）时才可能相等。`null` 指纹的语义是「这个超参必须为空」，比「通配任意值」严格——登记通配需要干脆不写这个键。

**练习 2**：为什么多命中（`len(matches) > 1`）默认抛异常而不是取第一个？

**参考答案**：多命中说明两条指纹的超参集合互为子集（例如一条登记了 8 个键、另一条 6 个键且都被满足），此时选哪个都可能是错的，而配置直接影响并行策略与算子行为，静默选错会造成难以定位的性能或正确性问题。deepseek_v3/v32 是作者明确知道重叠原因而留下的特判通道。

**练习 3**：jointfix 量化后的 92B 权重（`quantization_config.format == "int-quantized"`，权重/激活 8bit、KV bf16）在两个路由表中分别会命中哪个 precision？

**参考答案**：`parse_hf_config` 推导出 `w8a8c16`。high_throughout 表中没有 `openpangu_v2_92B + w8a8c16` 的条目（92B 只有 bf16 条目），会 warning + 默认配置；low_latency 表中虽有 `pangu_v2_moe_92B + w8a8c16` 条目（[L154-L183](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/best_practice_configs.json#L154-L183)），但受 4.2.3 所述模型名不一致影响同样匹配不上。这再次说明实际部署依赖 `CUSTOM_MODEL_CONFIG_PATH` 显式指定。

### 4.3 特性开关：features 后处理与 CUSTOM_MODEL_CONFIG_PATH 覆盖

#### 4.3.1 概念说明

json 加载完成后，配置还要过一道**后处理**（features.py）：根据运行模式裁剪不兼容的开关，例如 eager 模式下多流/预取类优化没有意义，会被强制关闭。这是「配置正确性」的最后防线。

而对于开发调测、问题规避等场景，系统提供**最高优先级的覆盖通道** `CUSTOM_MODEL_CONFIG_PATH`：设了它就完全跳过两级自动匹配，直接加载指定文件。部署模板大量使用这条通道来钉死行为。

优先级从高到低：

1. `CUSTOM_MODEL_CONFIG_PATH` 环境变量（相对 `configs/` 目录的路径）
2. `best_practice_configs.json` 自动匹配（受 `--additional-config` 的 `enable_low_latency` 影响选目录）
3. 三个配置类的代码默认值

#### 4.3.2 核心流程

```text
_init_model_extra_config(task_config):
    if 环境变量 CUSTOM_MODEL_CONFIG_PATH 存在:
        path = configs/ + CUSTOM_MODEL_CONFIG_PATH     # 注意：是相对路径拼接
        config_data = 读该 json                         # 跳过自动匹配
    else:
        config_data = _get_best_practice_config(task_config)   # 两级查表，可能为 None
    用 config_data 的两个键分别构造 ModelParallelConfig / ModelOperatorOptConfig
      （未知键被 filter_dict_by_dataclass 丢弃）
    挂到全局单例 model_extra_config 上

_validate_config(additional_config):                  # 后处理三连
    apply_eager_mode_config   → eager 模式关掉多流/超级算子/预取全家桶
    apply_omni_cache          → 当前为空实现（pass）
    apply_seq_parallel        → VLLM_PLUGINS 未含 omni_custom_models/omni_pangu_models
                                时强制关闭序列并行；开 CP 必须同时开 SP（assert）

_print_model_config():                               # 日志回显最终配置（JSON 全量）
```

#### 4.3.3 源码精读

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L315-L339](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L315-L339) —— `_init_model_extra_config` 全文：`CUSTOM_MODEL_CONFIG_PATH` 分支只做了 `os.path.join(default_config_path, 相对路径)`，因此**给定路径必须位于 configs 目录之内**；自动匹配返回 None 时走 else 分支挂默认配置；`ModelExtraConfig` 的三个成员在此被整体替换（单例对象不变，成员换新）。

- [components/omni-npu/src/omni_npu/model_config/config_loader/features.py:L15-L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/features.py#L15-L33) —— `apply_eager_mode_config`：`graph_mode == "eager_mode"` 时关掉 `enable_super_kernel`、`enable_prefetch` 并把所有预取大小归零。注意其中 `enable_scmoe_multi_stream` 一行指向不存在的字段（见 4.1.3 的观察），eager 下共享专家多流实际**没有**被这段关掉。

- [components/omni-npu/src/omni_npu/model_config/config_loader/features.py:L36-L49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/features.py#L36-L49) —— `apply_seq_parallel` 的双保险：序列并行依赖 `omni_custom_models` 插件里的通信域初始化（呼应 u2-l1 的三个插件），`VLLM_PLUGINS` 里没有就强制关闭并告警；`ena_context_parallel=True` 但 `ena_seq_parallel=False` 直接 assert 失败。

- [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L423-L429](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L423-L429) —— `_print_model_config`：把整个单例 `asdict` 后以 JSON 打进日志，行首文案 `ModelExtraConfig:`。**这是排障时确认「到底生效了什么」的权威出口**（json 里的拼写错误也要靠它发现）。

部署侧的两条通道在 1P1D BF16 模板里同时出现：

- [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L78-L86](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L78-L86) —— P 侧任务在第 83 行 `export CUSTOM_MODEL_CONFIG_PATH="low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json"`：不走自动匹配，直接钉死这份「open 版」P 侧配置（D 侧对应第 172 行钉 `_d_open.json`）。路径正是相对 `configs/` 的写法。第 92 行 `EXTRA_ARGS` 里的 `--enforce-eager` 还会让 `graph_mode='eager_mode'`，从而触发 4.3.3 第一条的 eager 裁剪——一个部署参数串起两段源码。

- [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L120-L124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L120-L124) —— 第 122 行 `--additional-config '{"enable_low_latency": true}'`：该参数经 pd_run.sh（[L233](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L233) 解析、[L409](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L409) 透传给 vllm serve）最终出现在 `vllm_config.additional_config` 里，被 `load_model_extra_config` 读走。README 中 `ADDITIONAL_CONFIG='{"enable_low_latency":true}'` 的说明（[model_config/README.md:L68-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L68-L77)）即指此机制。

#### 4.3.4 代码实践

**实践目标**：体验 `CUSTOM_MODEL_CONFIG_PATH` 的「最高优先级 + 相对路径」两个特性，并掌握用日志回显验证配置的方法。

**操作步骤**：

1. 容器内先用自动匹配（不设环境变量）加载一次，再钉死一份现有文件加载一次，对比单例内容（示例代码，非项目原有）：

```bash
docker exec -it <P容器名> bash
python -c "
import os
from omni_npu.model_config.config_loader.loader import (
    update_task_config, model_extra_config, _print_model_config)

# 先看默认值
print('默认 enable_mome_sp =',
      model_extra_config.operator_opt_config.enable_mome_sp)   # 预期 False

# 钉死 P 侧 open 版配置（路径相对 configs/ 目录）
os.environ['CUSTOM_MODEL_CONFIG_PATH'] = \
    'low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json'
update_task_config(model_name='openpangu_v2_92B', hardware_platform='A3',
                   quant_type='bf16', is_pd_disaggregation=True,
                   is_prefill_node=True)
print('加载后 enable_mome_sp =',
      model_extra_config.operator_opt_config.enable_mome_sp)   # 预期 True
print('加载后 moe_comm_strategy =',
      model_extra_config.operator_opt_config.moe_comm_strategy) # 预期 allgather_reducescatter
_print_model_config()   # 与服务日志里 ModelExtraConfig: 同款回显
"
```

2. 把环境变量改成一个不存在的路径（如 `low_latency/openpangu_v2/no_such.json`）再执行，观察报错文案。
3. 在已部署服务的 `LOG_PATH/server_0.log` 里搜索 `ModelExtraConfig:` 与 `Get custom_model_config_path from environ`，与上面第 1 步的回显对照。

**需要观察的现象**：第 1 步 `enable_mome_sp` 从 False 变 True（该文件第 11 行确为 true，见 [pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json:L9-L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json#L9-L22)）；第 2 步应得到 `_loader_configs_data` 包装的 RuntimeError（文件打不开走 `Exception` 分支）。

**预期结果**：确认覆盖通道优先于自动匹配、路径必须落在 `configs/` 之下、日志回显是最终事实标准。本段未实际运行，输出细节「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`CUSTOM_MODEL_CONFIG_PATH` 写成绝对路径 `/root/my.json` 会怎样？

**参考答案**：代码只做 `os.path.join(default_config_path, custom_model_config_path)`。拼接 `/root/my.json` 这种以 `/` 开头的路径时 `os.path.join` 会**丢弃前缀**，结果恰好还是 `/root/my.json`，能读到就生效——看似可用实属巧合；而写 `../xxx.json` 这类穿越路径同样会被原样拼出。README 明确要求「必须在 v1\models\config 路径下面」（[model_config/README.md:L76-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L76-L77)），正确姿势是始终给相对路径。

**练习 2**：某次排障你想临时关闭 MoE 权重预取来对比性能，有哪两种改法？各有什么代价？

**参考答案**：① 改部署：`export CUSTOM_MODEL_CONFIG_PATH` 指向一份你复制后修改的 json（把 `enable_prefetch` 设为 false），重启进程——不动源码、可回滚，但要注意 `__post_init__` 会联动把各预取大小归零（行为一致）；② 改源码默认值（`enable_prefetch: bool = False`）——影响所有未显式配置的模型，且升级即丢，不推荐。两法都应以日志 `ModelExtraConfig:` 回显确认生效。

**练习 3**：为什么 `apply_seq_parallel` 要检查 `VLLM_PLUGINS`？

**参考答案**：`ena_seq_parallel` 依赖 `omni_custom_models` 插件在启动早期建立的自定义层并行通信域（[npu_worker.py:L132-L134](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_npu/src/omni_npu/worker/npu_worker.py#L132-L134) 也只在插件列表含 `omni_custom_models` 时初始化）。若用户只加载了平台插件却通过 json 打开了序列并行，运行时会因通信域缺失而出错，与其晚失败不如加载期强制关闭并告警。

## 5. 综合实践

把本讲三个模块串起来，完成规格中设定的任务：**为假想的 `my-model` 写一份配置，走完「登记 → 覆盖加载 → 验证生效」全流程**。请在测试容器（或你自己的副本）中进行，结束后还原改动，避免污染部署环境。

**任务目标**：新模型 `my-model`（假想 `model_type="my_model"`、hidden_size=1024、8 头）能够被系统识别，并通过 `CUSTOM_MODEL_CONFIG_PATH` 加载你手写的最佳实践配置。

**步骤**：

1. **写配置文件**。仿照 [pangu_v2_moe_bf16_a3_xp1d_d.json](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_xp1d_d.json) 的结构，在 `components/omni-npu/src/omni_npu/model_config/configs/low_latency/openpangu_v2/` 下新建 `my_model_bf16_a3_1p1d_p.json`（示例代码，非项目原有文件）：

```json
{
    "model_parallel_config": {
        "ena_seq_parallel": false,
        "layer_parallel_config": {}
    },
    "operator_optimization_config": {
        "moe_comm_strategy": "allreduce",
        "enable_prefetch": false,
        "use_noncontiguous_kv": true
    }
}
```

2. **登记指纹**。在 `configs/match_hf_configs.json` 顶层对象里追加（键名即模型名，`null` 表示该超参须为空）：

```json
"my-model": {
    "model_type": "my_model",
    "hidden_size": 1024,
    "num_attention_heads": 8
}
```

3. **验证识别 + 覆盖加载**（容器内执行，示例代码）：

```bash
python -c "
import os
from types import SimpleNamespace
os.environ['CUSTOM_MODEL_CONFIG_PATH'] = \
    'low_latency/openpangu_v2/my_model_bf16_a3_1p1d_p.json'

from omni_npu.model_config.config_loader.loader import (
    parse_hf_config, update_task_config, model_extra_config, _print_model_config)

hf = SimpleNamespace(model_type='my_model', hidden_size=1024, num_attention_heads=8)
print('第一级匹配:', parse_hf_config(hf))          # 预期 ('my-model', 'bf16')

update_task_config(model_name='my-model', hardware_platform='A3',
                   quant_type='bf16', is_pd_disaggregation=True,
                   is_prefill_node=True, prefill_node_num=1, decode_node_num=1)
print('moe_comm_strategy =', model_extra_config.operator_opt_config.moe_comm_strategy)
print('expert_gate_up_prefetch =', model_extra_config.operator_opt_config.expert_gate_up_prefetch)
_print_model_config()
"
```

4. **对照预期**：`parse_hf_config` 返回 `('my-model', 'bf16')`；`moe_comm_strategy = allreduce`（来自你的 json）；`expert_gate_up_prefetch = 0`（json 里 `enable_prefetch=false` 触发 `__post_init__` 联动归零——json 并没有写预取大小，是校验钩子替你补齐的一致性）；日志回显中 task_config 的 model_name 为 `my-model`。
5. **（进阶，可选）** 按 [model_config/README.md:L79-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L79-L100) 的第 3、4 步，在 `high_throughout/best_practice_configs.json` 里为 `my-model` 增加一条 `A3 + bf16 + 1P1D` 路由，然后**不设** `CUSTOM_MODEL_CONFIG_PATH` 重新运行第 3 步，验证自动匹配也能命中你的文件（此时 `_print_model_config` 前的日志会打印 "load configuration file from ..."）。
6. **清理**：删除新建 json、还原 `match_hf_configs.json`（若做了第 5 步也还原路由表），`git status` 确认工作区干净。

**预期结果**：你将同时验证三个模块的知识——分层（哪个键进哪个配置类）、识别（指纹登记与命中）、开关（覆盖通道优先级与 post_init 联动）。所有输出「待本地验证」。

## 6. 本讲小结

- **配置分层**：`ModelExtraConfig` 单例聚合 `TaskConfig`（环境事实，运行时推导）、`ModelParallelConfig`（并行策略）、`ModelOperatorOptConfig`（算子优化开关，含 `moe_comm_strategy`、预取家族等）；json 文件只装载后两者，加载时机在 `NPUWorker.init_device` 中模型构建之前。
- **模型识别**：两级查表——`match_hf_configs.json` 用架构超参指纹把 `model_type` 细化成规格名（如 `openpangu_v2_92B`），`best_practice_configs.json` 再按「模型 + 硬件（A2/A3/A5）+ 精度（bf16/w8a8c16/...）+ PD 形态（1P1D/hybrid/...）」路由到具体文件，P/D 两侧各取一份。
- **匹配失败不致命**：自动匹配落空只打 warning 并回退到代码默认值；但登记了路由而文件缺失会直接 RuntimeError。
- **源码观察**：low_latency 路由表中 openPangu 条目的 `model` 字段（`pangu_v2_moe_92B` 等）与第一级匹配输出的名字（`openpangu_v2_92B` 等）不一致，低时延自动匹配实际落入默认配置分支——因此生产模板一律用 `CUSTOM_MODEL_CONFIG_PATH` 显式钉死文件。
- **特性开关**：`features.py` 后处理负责 eager 模式裁剪与序列并行门禁（依赖 `omni_custom_models` 插件）；`--additional-config` 的 `enable_low_latency` 决定查 high_throughout 还是 low_latency 目录。
- **排障入口**：日志中 `ModelExtraConfig:` 的 JSON 回显是「最终生效配置」的权威事实；`filter_dict_by_dataclass` 会静默丢弃拼错的 json 键，改配置后务必以回显为准。

## 7. 下一步学习建议

本讲搞定了「配置如何被选中与加载」，接下来两个自然方向：

1. **u5-l2 图编译：ACL Graph 与 GE 后端**——本讲多次出现的 `graph_mode`（eager_mode / acl_graph / ge_graph）正是在那里被消费：`enforce-eager` 与图模式的性能对比、`use_gegraph` 的 GE 编译链路。你会发现 ansible 模板里 `--enforce-eager` 与本讲 eager 裁剪逻辑的联动只是图编译故事的序幕。
2. **u10-l3 二次开发：新增补丁、模型配置与连接器**——本讲综合实践只走了 `CUSTOM_MODEL_CONFIG_PATH` 临时通道；那一讲会练习「正式新增一个模型」的完整 upstream 流程（含路由登记与 UT 唯一性校验），以及另两类扩展点。

建议继续精读的源码：`configs/low_latency/openpangu_v2/` 下其余变体（`_perf` / `_claw` / `_omnicache` 系列），对比它们与 `_open` 版的字段差异，思考每个差异对应哪类部署场景（性能压测、OmniCache 组网等），这是理解「最佳实践」如何随场景演化的最好材料。
