# u6-l2 csrc 适配层：Autograd Function 与算子注册

## 1. 本讲目标

上一讲（u6-l1）我们看清了 torch_ops_extension 扩展包的整体骨架：`setup.py` 收集 csrc 源码、`csrc_base` 公共层提供 schema 注册与符号查找、`__init__.py` 完成挂载。本讲钻进**单个算子的 csrc 适配文件**，以 `npu_aggregate_hidden.cpp` 为标本，逐行回答三个问题：

1. `EXEC_NPU_CMD_V1` 这一行宏，是如何把 `at::Tensor` 变成 aclnn 调用并异步下发到 NPU 的？
2. `torch::autograd::Function` 子类如何把「前向算子 + 反向算子」两个独立的 aclnn 接口拼接成一个可自动求导的 PyTorch 算子？
3. `TORCH_LIBRARY_IMPL(custom, PrivateUse1 / AutogradPrivateUse1 / Meta, m)` 三路注册各拦截什么请求？`__init__.py` 又是怎么把 `torch.ops.custom.*` 挂到 `torch_npu` 上的？

学完本讲，你应该能独立为一个已有 aclnn 接口的算子写出完整的 csrc 适配（NPU 实现 + Autograd Function + Meta 实现 + 三路注册）。

## 2. 前置知识

### 2.1 PyTorch Dispatcher 与 dispatch key（通俗版）

PyTorch 里每次调用 `torch.add(a, b)`，并不是直接执行加法函数，而是先经过一个**分发器（Dispatcher）**：它检查输入张量的设备（CPU/NPU/meta）、是否需要梯度等特征，把这些特征映射成一组 **dispatch key**，再按优先级找到该 key 下注册的实现函数。可以把分发器理解成火车站的调度台，dispatch key 就是车次牌——同一张车票（算子 schema），不同车次（key）走不同轨道（实现）。

本讲涉及三个关键 key：

| dispatch key | 含义 | 谁注册 |
|---|---|---|
| `PrivateUse1` | 预留给第三方后端的设备 key，torch_npu 用它表示 NPU 设备 | 真正干活的 NPU 实现 |
| `AutogradPrivateUse1` | `PrivateUse1` 后端之上的 autograd 层 | 记录反向图的 Autograd 包装 |
| `Meta` | 「meta 设备」：只有 shape 没有 data 的假张量，用于形状推导 | 只分配输出 shape 的伪实现 |

一个输入在 NPU 上且 `requires_grad=True` 的调用，会先命中 `AutogradPrivateUse1`；`requires_grad` 全为 False 时直接命中 `PrivateUse1`，零 autograd 开销。

### 2.2 C++ 版 torch::autograd::Function

Python 侧我们写过 `class MyFunc(torch.autograd.Function)` 并实现 `forward`/`backward`。C++ 侧是同一套机制的模板版（头文件 `torch/csrc/autograd/custom_function.h`）：

```cpp
class MyFunction : public torch::autograd::Function<MyFunction> {
  static variable_list forward(AutogradContext *ctx, /*前向参数*/...);
  static variable_list backward(AutogradContext *ctx, variable_list grad_outputs);
};
// 调用：MyFunction::apply(args...)
```

两条铁律（本讲反复用到）：

- **backward 的返回个数必须等于 forward 的参数个数**，顺序一一对应；对不需要梯度的参数（如 bool 掩码、int 属性），返回默认构造的 `at::Tensor()`（即「未定义张量」，等价于 Python 返回 `None`）。
- 前向想留给反向用的张量必须经 `ctx->save_for_backward({...})` 保存，反向用 `ctx->get_saved_variables()` 取回——直接存成员变量会被 autograd 的版本计数机制「宽恕」掉。

### 2.3 回顾：aclnn 两段式接口（u2-l5）

aclnn 接口分两段：第一段 `aclnnXxxGetWorkspaceSize(...)` 在 Host 上校验参数、触发 tiling、把整条执行计划记入 `aclOpExecutor` 并回填 workspace 大小；第二段 `aclnnXxx(workspace, workspaceSize, executor, stream)` 把任务异步下发到 stream。本讲的 `EXEC_NPU_CMD_V1` 就是这两段调用的 C++ 模板化封装。

### 2.4 c10::optional

`c10::optional<at::Tensor>` 是 C++17 `std::optional` 的 PyTorch 别名，表示「可能有值也可能没有」。schema 里的 `Tensor? mask=None` 在 C++ 签名里就对应 `const c10::optional<at::Tensor> &mask`。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp) | **本讲主标本**：aggregate_hidden 的完整 csrc 适配（149 行），包含 NPU 前反向、Autograd Function、Meta、三路注册 |
| [omni_training_custom_ops/csrc_base/ops_common.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h) | 公共桥接层：acl 符号 dlopen/dlsym 解析、at::Tensor→aclTensor 类型转换、`EXEC_NPU_CMD_V1` 宏 |
| [omni_training_custom_ops/csrc_base/ops_common.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.cpp) | 桥接层的全局初始化与补充符号查找链 |
| [omni_training_custom_ops/csrc_base/ops_def_registration.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp) | schema 总注册表（`TORCH_LIBRARY_FRAGMENT(custom, m)`）与 pybind11 绑定，是三路 `m.impl` 的前提 |
| [omni_training_custom_ops/csrc_base/function.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/function.h) | 各算子 autograd 封装函数的跨编译单元声明 |
| [omni_training_custom_ops/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py) | 包入口：import 触发注册 + 把 `torch.ops.custom.*` 挂载到 `torch_npu` |
| [omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp) | 对照样本：仓库已有的 sinkhorn csrc（**没有** Autograd 层），是综合实践的参考底版 |
| [ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp) | 算子原型（u2-l2 已精读），本讲用它验证参数顺序契约 |

阅读本讲前建议先回顾 u2-l5（aclnn 两段式）与 u6-l1（扩展包总览）。

## 4. 核心概念与源码讲解

### 4.1 EXEC_NPU_CMD_V1：一行宏背后的 aclnn 桥接层

#### 4.1.1 概念说明

csrc 适配层与 CANN 之间隔着一条「类型鸿沟」：PyTorch 世界里是 `at::Tensor`、`int64_t`、`c10::optional`，aclnn 世界里是 `aclTensor *`、`aclOpExecutor *`、裸指针。而且本仓库绝大多数算子**没有 op_api 源码入仓**（u1-l2 结论），`aclnnXxx` 符号只存在于已安装算子包的 `libcust_opapi.so` 里，编译期根本链接不到。

`csrc_base/ops_common.h` 就是横跨这条鸿沟的桥，它做三件事：

1. **运行期符号解析**：用 `dlopen`/`dlsym` 按名字在若干候选 `.so` 中查找 aclnn 函数指针——这就是 u6-l1 说的「EXEC_NPU_CMD_V1 按名动态解析补齐 aclnn 符号」的实现处。
2. **参数类型转换**：一组 `ConvertType` 重载把 `at::Tensor`、标量、optional 统一转成 acl 类型，调用完再用一组 `Release` 重载销毁。
3. **两段式调用封装**：`EXEC_NPU_CMD_V1` 宏串起「取 stream → 转参数 → 第一段 GetWorkspaceSize → 分配 workspace → 第二段执行 → 释放」全流程。

#### 4.1.2 核心流程

`EXEC_NPU_CMD_V1(aclnn_api, args...)` 展开后的执行序列（伪代码）：

```text
EXEC_NPU_CMD_V1(aclnnXxx, t1, t2, ..., out1):
  1. static 解析符号（仅首次）:
       getWorkspaceSizeFuncAddr = GetOpApiFuncAddr("aclnnXxxGetWorkspaceSize")
       opApiFuncAddr            = GetOpApiFuncAddr("aclnnXxx")
       + InitHugeMemThreadLocal / ReleaseHugeMem / InitPTACache... 等辅助符号
  2. acl_stream = 当前 NPU stream（异步下发目标）
  3. converted_params = ConvertTypes(t1, t2, ..., out1, &workspace_size, &executor)
       │  at::Tensor        → aclTensor*
       │  optional<Tensor>  → aclTensor* 或 nullptr
       │  int64_t/double    → 原样透传
  4. 调用第一段 aclnnXxxGetWorkspaceSize(converted_params...)
       │  CANN 侧: 校验 → tiling → 构建 executor → 回填 workspace_size
  5. workspace_size != 0 时: 在 NPU 上 at::empty 分配 byte 张量
  6. lambda acl_call: 调用第二段 aclnnXxx(workspace, size, executor, stream)
       │  成功后 ReleaseConvertTypes 销毁所有 acl 对象
  7. OpCommand::RunOpApiV2 包裹 lambda 统一下发（异常/_PROFILING 处理）
```

符号解析链（`GetOpApiFuncAddr`）的查找顺序，体现了「自定义算子优先、CANN 内置兜底」的策略：

```text
① ASCEND_CUSTOM_OPP_PATH 各路径 + "/op_api/lib/libcust_opapi.so"   ← 自装算子包
② ASCEND_OPP_PATH/vendors/config.ini 的 load_priority 各厂商 + 同上  ← 按优先级
③ CANN 特征库 libopapi_math/nn/cv/transformer/legacy.so
④ libopapi.so 主库
⑤ libaclnn_ops_infer/train/math/sparse/fft/rand.so（ops_common.cpp 补充链）
```

#### 4.1.3 源码精读

**符号解析的准备：库路径来自环境变量。** `get_custom_lib_path` 读取 `ASCEND_CUSTOM_OPP_PATH`（冒号分隔多路径），为每条路径拼上 `/op_api/lib/` 后缀：

- [ops_common.h:209-230](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L209-L230)：解析 `ASCEND_CUSTOM_OPP_PATH` 为候选库目录列表；变量不存在时只打 warning 并返回空列表。
- [ops_common.h:232-275](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L232-L275)：第二候选来源——读 `ASCEND_OPP_PATH/vendors/config.ini` 中的 `load_priority=` 行，按逗号拆出厂商加载顺序。
- [ops_common.cpp:13-14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.cpp#L13-L14)：两个路径列表是**全局变量，在 .so 被加载（import）的瞬间完成初始化**。这就是 u6-l1 强调「必须先装算子 run 包并 source set_env.bash、再 import 本包」的代码级原因——import 之后再生效的环境变量不会被读到。

**dlopen/dlsym 的落点：**

- [ops_common.h:292-308](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L292-L308)：`GetOpApiFuncAddrInLib` 用 `dlsym` 在已打开句柄里找符号；`GetOpApiLibHandler` 用 `dlopen(..., RTLD_LAZY)` 打开 `.so`。失败只记 warning 返回 nullptr，由上层决定报错。
- [ops_common.h:325-379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L325-L379)：`GetOpApiFuncAddr` 主体——按上文 ①→④ 顺序遍历查找，任何一步命中即返回函数地址。

**类型转换：at::Tensor → aclTensor。**

- [ops_common.h:451-516](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L451-L516)：`ConvertType(const at::Tensor &)` 的核心逻辑——dtype 查 [ops_common.h:168-173](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L168-L173) 的映射表（如 `Half→ACL_FLOAT16`、`BFloat16→ACL_BF16`，见 [ops_common.h:146-166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L146-L166)），再取 sizes/strides/storage_offset/格式（3 维→NCL、4 维→NCHW、5 维→NCDHW），最后用运行期解析到的 `aclCreateTensor` 构造 `aclTensor*`。注意它**不拷贝数据**，只传 `storage().data()` 指针——零拷贝桥接。
- [ops_common.h:635-642](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L635-L642)：`c10::optional<at::Tensor>` 的特化——空值直接返回 `nullptr`。这解释了主标本里「mask 为空直接传」的写法：aclnn 侧的可选张量就用 NULL 表示缺席。
- [ops_common.h:769-773](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L769-L773)：模板透传重载 `ConvertType(T value) { return value; }`——`int64_t`、`double` 等标量原样通过，因为 aclnn C 接口的属性参数本来就是原生 C 类型。
- [ops_common.h:873-877](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L873-L877)：`ConvertTypes(Ts &... args)` 把所有实参打包成 tuple，配合 [ops_common.h:879-890](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L879-L890) 的 `call` 展开调用——一套 C++14 风格的「编译期参数转发」。

**宏本体：**

- [ops_common.h:965-1024](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965-L1024)：`EXEC_NPU_CMD_V1` 全貌。几个关键行：
  - L967-968 用字符串拼接 `#aclnn_api "GetWorkspaceSize"` 解析两段符号，`static const auto` 保证整个进程只解析一次；
  - L974-976 `TORCH_CHECK`：任何一个符号找不到，立刻报出「`aclnnXxx or aclnnXxxGetWorkspaceSize not in libopapi.so`」——这是排查「算子包装好了但 run 包没装/没 source」时最常见的报错形态；
  - L995 把 `workspace_size_addr`、`executor_addr` 两个指针**追加到参数元组尾部**，这正是 aclnn 第一段函数签名的最后两个出参；
  - L997 调用第一段；L1001-1007 按 `workspace_size` 分配 workspace；L1008-1018 定义第二段调用的 lambda（内含 `ReleaseConvertTypes` 释放 acl 对象）；L1019 `OpCommand::RunOpApiV2` 统一下发。

一个值得指出的**阅读陷阱**：[ops_common.h:999-1007](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L999-L1007) 中外层先声明了 `at::Tensor workspace_tensor;`，而 if 块内 `auto workspace_tensor = at::empty(...)` 是一个**同名遮蔽**——内层张量在 if 块结束时析构，外层始终为空，`workspace_addr` 的生命周期靠 NPU 缓存分配器的迟回收特性兜底。这段代码沿袭自 torch_npu 上游同款实现，读码时不要误以为外层声明真的延长了 workspace 生命周期。另外 [ops_common.h:1027-1092](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L1027-L1092) 还有一个旧版 `EXEC_NPU_CMD_v0`（小写 v），差异在错误信息带 `aclGetRecentErrMsg()`、workspace 分支同样有遮蔽问题；本仓库 csrc 全部使用 V1 版。

#### 4.1.4 代码实践

**实践目标**：不写新代码，只通过「符号解析失败」实验验证你对查找链的理解。

**操作步骤**：

1. 在已安装 run 包并 `source set_env.bash` 的容器里（u1-l4 步骤），先确认变量存在：`echo $ASCEND_CUSTOM_OPP_PATH`（应指向 `.../opp/vendors/omni_training_custom_transformer`）。
2. `python3 -c "import torch, torch_npu, omni_training_custom_ops"`，确认 import 成功。
3. 反向实验：`ASCEND_CUSTOM_OPP_PATH= python3 -c "...同上..."`（把变量置空再 import）。
4. 观察步骤 3：import 大概率仍成功（路径函数只打 warning），但接下来调用 `torch.ops.custom.npu_aggregate_hidden(...)` 时会在 `TORCH_CHECK` 处抛出 `aclnnAiInfraAggregateHidden or aclnnAiInfraAggregateHiddenGetWorkspaceSize not in libopapi.so...`。

**需要观察的现象**：报错文本正是 [ops_common.h:974-976](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L974-L976) 拼出来的字符串；而同样的调用在步骤 2 的环境下能正常出结果。

**预期结果**：你亲手复现了「环境变量在 import 前必须就位」这条约束的报错形态。无 NPU 环境时可跳过运行，改为在文中写出该报错出自哪一行宏展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EXEC_NPU_CMD_V1` 里的符号解析用 `static const auto` 而不是每次调用都 `dlsym`？

**答案**：`dlopen`/`dlsym` 是带锁的动态链接器操作，开销远高于普通函数调用；而 `.so` 一旦加载，符号地址在整个进程生命周期内不变。`static` 保证首次调用解析、后续调用直接用缓存的函数指针，把解析成本摊销为一次。

**练习 2**：`ConvertType` 对 `c10::optional<at::Tensor>` 空值返回 `nullptr` 而不是报错，这个设计决定了 csrc 层可以怎样写可选参数？

**答案**：决定了 csrc 层可以「无脑透传」optional——不必先判断 mask 是否存在再写两个分支，直接把 optional 塞进 `EXEC_NPU_CMD_V1` 参数表，空值自动变成 aclnn 侧约定的 NULL 可选张量。主标本 L31 的注释「mask为空直接传」说的就是这件事。

**练习 3**：如果把 `npu_aggregate_hidden` 的调用换成 CPU 张量输入，会在哪一层失败？

**答案**：不会走到 `EXEC_NPU_CMD_V1`。输入在 CPU 设备上，Dispatcher 计算出的 key 是 CPU 后端而非 `PrivateUse1`，而我们只在 `PrivateUse1`/`AutogradPrivateUse1`/`Meta` 下注册了实现，分发器找不到 CPU 实现会直接抛 "NotImplementedError"（同类报错也可能来自 `ConvertType(const TensorWrapper &...)` 里的 `is_npu` 检查，见 [ops_common.h:703-706](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L703-L706)，但那是对 TensorWrapper 重载；主路径失败在 dispatch 阶段）。

### 4.2 NPU 前向与反向实现：输出张量准备与参数顺序契约

#### 4.2.1 概念说明

`npu_aggregate_hidden.cpp` 的最内层是两个「干活的」普通函数：`npu_aggregate_hidden_npu`（前向）与 `npu_aggregate_hidden_grad_npu`（反向）。它们职责单一：

1. 在 Host 上用 `at::empty_symint` **预分配输出张量**（aclnn 约定：输出内存由调用方分配，aclnn 只往里写）；
2. 把输入、输出按 **`_def.cpp` 声明顺序**排成一列，交给 `EXEC_NPU_CMD_V1`。

这里有一条贯穿四层的**参数顺序契约**：csrc 的 `EXEC_NPU_CMD_V1(...)` 参数顺序 = `_def.cpp` 里 `Input/Output` 的声明顺序 = aclnn 生成代码的形参顺序 = tiling 侧 `INPUT_INDEX` 的取值依据（u2-l2 讲过）。任何一层调序，其余各层必须同步，否则张量身份错位、算子「能跑但算错」，是最难排查的一类静默错误。

#### 4.2.2 核心流程

前向（aggregate_hidden 是 \( y[s,b,h] = \sum_{w=0}^{2} \text{weight}[w,h] \cdot x[s+w-2, b,h] \) 的一维分组卷积，输出 shape 等于输入 shape）：

```text
npu_aggregate_hidden_npu(input[S,B,H], weight[W,H], mask[B,S]?):
  output = at::empty_symint([S,B,H], options=input)   # shape/dtype 继承 input
  EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden,
                  input, weight, mask, output)         # 3 输入 + 1 输出，def 顺序
  return output
```

反向（输出两路梯度）：

```text
npu_aggregate_hidden_grad_npu(grad_output, input, weight, mask?):
  grad_input  = at::empty_symint([S,B,H], options=input)
  grad_weight = at::empty_symint([W,H],  options=input)   # 注意：options 跟 input
  EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHiddenGrad,
                  grad_output, input, weight, mask,
                  grad_input, grad_weight)                 # 4 输入 + 2 输出
  return (grad_input, grad_weight)
```

#### 4.2.3 源码精读

- [ai_infra_aggregate_hidden_def.cpp:24-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L42)：原型侧的声明顺序——`Input("input")`、`Input("weight")`、`Input("mask")`、`Output("output")`，无 Attr。这就是 csrc 参数表的唯一权威依据。
- [npu_aggregate_hidden.cpp:23-34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L23-L34)：前向实现。L27-28 用 `std::vector<c10::SymInt>` + `at::empty_symint` 分配输出——用 SymInt 而非 int64_t 是为了兼容动态 shape（torch.compile 场景）；L31 一行宏完成调用，L31 行尾注释「mask为空直接传」对应 4.1 讲的 optional→nullptr 机制。
- [npu_aggregate_hidden.cpp:37-50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L37-L50)：反向实现。L41-44 分别按 `input`、`weight` 的 shape 分配两个梯度张量；注意 L44 `grad_weight` 的 options 也取自 `input`（训练中 input/weight 同 dtype 时无差别，这是一种实现选择）。L47 调用 `aclnnAiInfraAggregateHiddenGrad`，参数 `grad_output, input, weight, mask, grad_input, grad_weight` 同样遵循反向算子 def 的声明顺序。
- 对照一个带属性的例子：[npu_sinkhorn.cpp:110-111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp#L110-L111) 中 `EXEC_NPU_CMD_V1(aclnnManifoldConstrainedHyperConnectionSinkhornEnhance, x, out_flag, eps, num_iters, output, norm_out, sum_out)`——1 个输入 + 3 个标量属性 + 3 个输出，与 [manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp:23-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mhc/manifold_constrained_hyper_connection_sinkhorn_enhance/op_host/manifold_constrained_hyper_connection_sinkhorn_enhance_def.cpp#L23-L46) 的 `Input("x")` + `Attr("out_flag"/"eps"/"num_iters")` + 三个 Output 一一对应。属性（标量）混在张量之间按声明顺序排即可，`ConvertType` 的透传重载负责放行。

#### 4.2.4 代码实践

**实践目标**：用「参数顺序契约」做一次跨层核对，体会四层对齐。

**操作步骤**：

1. 打开 [ai_infra_aggregate_hidden_def.cpp:24-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L42)，抄下 Input/Output 顺序：input, weight, mask → output。
2. 打开 [npu_aggregate_hidden.cpp:31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L31) 与 [npu_aggregate_hidden.cpp:47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L47)，核对两处实参顺序。
3. 再任选一个你没读过的算子（如 `ops_transformer/mhc/sinkhorn_grad/csrc/npu_sinkhorn_grad.cpp`），先看它的 def（`ascendc/src/ops-transformer/mhc/ai_infra_sinkhorn_grad/op_host/ai_infra_sinkhorn_grad_def.cpp`）猜出 `EXEC_NPU_CMD_V1` 参数表，再翻到 [npu_sinkhorn_grad.cpp:26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn_grad/csrc/npu_sinkhorn_grad.cpp#L26) 对答案（应为 `grad_output, norm_out, sum_out, grad_input`）。

**需要观察的现象**：猜中的顺序与实际一致；若某个 def 里存在 OPTIONAL 输出（如 sinkhorn 的 norm_out/sum_out），csrc 侧也必须为它分配张量并占位传参。

**预期结果**：产出一张「def 声明顺序 ↔ EXEC_NPU_CMD_V1 实参」对照表，作为后续自己写 csrc 时的核对模板。本实践为纯源码阅读型，无需 NPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么输出张量必须在 csrc 侧预分配，而不是像某些框架那样由算子返回？

**答案**：aclnn 的两段式约定输出缓冲由调用方提供：第一段 `GetWorkspaceSize` 需要完整参数（含输出张量描述）来做 tiling 与 executor 构建，第二段只往这些缓冲里写。因此 csrc 必须先 `at::empty_symint` 造好输出再调宏。

**练习 2**：`at::empty_symint` 与 `at::empty` 的区别是什么？这里为什么选前者？

**答案**：前者接受 `c10::SymInt`（符号整数）尺寸，支持动态 shape 推导（如 torch.compile / meta 场景下 shape 是符号表达式）；后者只接受具体 int64。csrc 层为了同时服务 eager 与编译捕获两种模式，统一用 SymInt 版本。

**练习 3**：如果把 `npu_aggregate_hidden.cpp:47` 中 `grad_input` 与 `grad_weight` 两个实参对调，会发生什么？

**答案**：编译通过、运行大概率也不报错——aclnn 会把写到 `grad_input` 缓冲的数据当成 weight 梯度（shape [W,H] 与 [S,B,H] 不匹配时第一段校验可能报错；若恰好同 numel 则静默算错）。这正是参数顺序契约失配的典型「静默出错」形态。

### 4.3 Autograd Function：forward 保存现场、backward 拼接梯度算子

#### 4.3.1 概念说明

有了 4.2 的两个 NPU 函数，用户已经可以手动前向、手动反向。但训练脚本里要的是 `loss.backward()` 自动反传——这要求把两个独立算子「焊」进 PyTorch 的计算图。`AiInfraAggregateHiddenFunction` 就是焊点：

- `forward`：调前向算子，并**保存反向所需的现场**（input、weight、mask，以及前向输出 output）；
- `backward`：收到上游梯度 `grad_output`，调反向算子得到 `grad_input`/`grad_weight`，按 forward 参数顺序返回。

对 aggregate_hidden 而言数学关系是：设前向 \( y = f(x, w) \odot m \)（m 为 bool 掩码，逐 (b,s) 位置门控），则

\[ \frac{\partial L}{\partial x} = m \odot \frac{\partial L}{\partial y} \cdot \frac{\partial f}{\partial x}, \qquad \frac{\partial L}{\partial w} = \sum_{s,b} \frac{\partial L}{\partial y_{s,b}} \cdot m_{b,s} \cdot \frac{\partial f}{\partial w} \]

——即掩码的梯度没有定义：\(m\) 是 0/1 离散选择子，\(y\) 对 \(m\) 的导数不存在，且 bool 张量在 PyTorch 中根本不能 `requires_grad`。backward 对 mask 位置返回 `at::Tensor()`（未定义张量 = None），autograd 引擎据此跳过向 mask 的传播。

#### 4.3.2 核心流程

```text
用户: y = torch.ops.custom.npu_aggregate_hidden(x, w, mask)   # x.requires_grad=True
  └─ Dispatcher → AutogradPrivateUse1 → npu_aggregate_hidden_autograd
       └─ AiInfraAggregateHiddenFunction::apply(x, w, mask)
            ├─ forward(ctx, x, w, mask):
            │    guard = AutoDispatchBelowADInplaceOrView      # 关闭 autograd 相关 key
            │    op = findSchemaOrThrow("custom::npu_aggregate_hidden")
            │    output = op.call(x, w, mask)                 # 直接落到 PrivateUse1 实现
            │    ctx->save_for_backward({output.detach(), x, w, mask 或空张量})
            │    return {output}                               # 建立反向节点
            └─ (反向时机) backward(ctx, grad_outputs):
                 saved = ctx->get_saved_variables()            # 取回现场
                 grad = grad_outputs[0].defined() ? grad.contiguous() : zeros_like(output)
                 grads = op_grad.call(grad, x, w, mask)        # 调反向算子
                 return {grad_input, grad_weight, at::Tensor()}  # mask → None
```

#### 4.3.3 源码精读

**forward：三步走。**

- [npu_aggregate_hidden.cpp:53-72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L53-L72)：
  - L58 `at::AutoDispatchBelowADInplaceOrView guard;`——RAII 守卫，临时压低 autograd/inplace-view 相关 dispatch key。没有它，L65 的 `op.call` 会再次命中 `AutogradPrivateUse1`（输入仍带着 requires_grad），形成「autograd 包装调 autograd 包装」的无限递归；有了它，调用直接落到 `PrivateUse1` 的真实计算实现。
  - L61-63 `torch::Dispatcher::singleton().findSchemaOrThrow("custom::npu_aggregate_hidden", "").typed<decltype(npu_aggregate_hidden_npu)>()`——按名查 schema 并铸成类型安全的调用句柄；`static` 表示只查一次。`.typed<decltype(...)>` 用前向函数指针类型做编译期签名校验，签名不一致直接编译失败。
  - L68 `ctx->save_for_backward({output.detach(), input, weight, mask.value_or(at::Tensor())})`——保存四个张量；`output.detach()` 切断它自身的梯度追踪；`mask.value_or(at::Tensor())` 把 optional 归一成「有值张量或未定义张量」以便塞进统一列表。
  - L71 `return {output};`——返回值装进 `variable_list`，apply 的调用方拿到 output，同时 autograd 已在图上挂好 `backward` 节点。

**backward：四步走。**

- [npu_aggregate_hidden.cpp:74-109](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L74-L109)：
  - L77-83 取回保存的变量；L83 `saved[3].defined() ? optional(saved[3]) : nullopt` 把 mask 还原成 optional（与保存时的归一互逆）。
  - L86-91 梯度预处理：上游梯度存在则 `contiguous()`（NPU 算子对内存连续性的硬要求，呼应 def 的 `AutoContiguous()`，u2-l2）；未定义（比如 output 不是 loss 的函数时）则补 `zeros_like(output)`——保证反向算子拿到的永远是有效缓冲。
  - L94-98 与 forward 对称地查 `custom::npu_aggregate_hidden_grad` 并调用（该 schema 声明在 [ops_def_registration.cpp:45-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L45-L46)：`(Tensor grad_output, Tensor input, Tensor weight, *, Tensor? mask=None) -> (Tensor, Tensor)`）。
  - L104-108 返回三元组：`input_grad`、`weight_grad`、`at::Tensor()`。第三个未定义张量就是 mask 的「None 梯度」——backward 返回个数必须与 forward 参数个数（3 个）对齐，autograd 引擎把未定义张量视为「此路不通」，且 mask 是 bool 张量本就无法 require grad，两侧语义一致。

**autograd 包装与 Meta 实现。**

- [npu_aggregate_hidden.cpp:113-119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L113-L119)：`npu_aggregate_hidden_autograd` 薄包装，把 `Function::apply` 的返回列表取第一个——签名与前向 NPU 函数完全相同（这一点很重要：它注册在 AutogradPrivateUse1 下，要能顶替前向函数的位置）。
- [npu_aggregate_hidden.cpp:122-129](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L122-L129)：Meta 实现——只按 input 的 shape 分配输出，不触发任何 NPU 调用。对照 [npu_sinkhorn.cpp:26-93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp#L26-L93) 可以看到当输出 shape 需要真实推导时，Meta 与 NPU 实现会**复用同一个 shape 构造函数**（`construct_sinkhorn_output_tensor`），这是避免两处 shape 逻辑漂移的推荐写法。

一个自然的问题：**为什么 backward 不直接调用同文件的 `npu_aggregate_hidden_grad_npu`，而绕 Dispatcher 一圈？** 因为经 Dispatcher 调用能正确处理 dispatch key（例如反向发生在 no_grad 环境下也能落到正确实现），并让 autograd 函数与具体实现解耦——`findSchemaOrThrow + typed` 的静态缓存把这个间接性成本压到一次。

#### 4.3.4 代码实践

**实践目标**：用 PyTorch 的自动求导机制验证「mask 无梯度」并量化梯度的正确性。

**操作步骤**：

1. 写一个 CPU 上的黄金参考（承接 u2-l1 的 numpy golden，这次用 torch）：`weight` 形状 [3,H]，`input` 形状 [S,B,H] `requires_grad=True`，按因果卷积公式手写前向（mask 用 `*` 乘上去）。
2. `loss = ref(input, weight, mask).sum(); loss.backward()` 后打印 `input.grad`、`weight.grad`；再试 `mask.requires_grad = True`，观察 torch 的行为。
3. （有 NPU 时）在容器内跑真算子：`y = torch.ops.custom.npu_aggregate_hidden(x.npu(), w.npu(), mask.npu()); y.sum().backward()`，比较 `x.grad`、`w.grad` 与步骤 2 的 CPU 参考。

**需要观察的现象**：

- 步骤 2 中对 bool mask 设 `requires_grad=True` 会直接报错（只有浮点/复数叶张量可求梯度）——从框架层面印证 4.3.1 的结论；
- 若你把参考实现里的 mask 换成 float 张量（0.0/1.0），backward 能算出 `mask.grad = grad_y * conv_result`，这恰好说明「bool 掩码无梯度」是 dtype 语义限制，而非公式里缺一项。

**预期结果**：CPU 参考与 NPU 算子的 `input.grad`/`weight.grad` 数值一致（bf16 有容差）；mask 一律无梯度。无 NPU 环境时完成步骤 2 即可，步骤 3 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：forward 里保存了 `output`，但反向公式并不直接用它算梯度，为什么还要存？

**答案**：两个用途：其一，backward L90 在上游梯度未定义时用 `at::zeros_like(output)` 补零缓冲，需要 output 的 shape/dtype/device 信息；其二，把 output 存进 ctx 是这类适配的通用习惯——万一反向需要前向中间量（如 FA 反向要复用 softmax_max/sum，u4-l4），接口已经就位。

**练习 2**：`AutoDispatchBelowADInplaceOrView guard` 去掉后，最直接的风险是什么？

**答案**：`op.call(input, weight, mask)` 会再次命中 `AutogradPrivateUse1` 键上注册的 `npu_aggregate_hidden_autograd`，后者再次 `Function::apply`，无限递归直至栈溢出。守卫的本质是在「记录图」的世界里临时打开一条「只算不计」的通道。

**练习 3**：如果 forward 多保存了一个没用的超大张量，代价是什么？如何取舍？

**答案**：`save_for_backward` 的张量在反向结束前一直占着显存，无脑保存会把前向的显存峰值转嫁到整个训练 step。取舍原则：只存反向公式真正消费的张量；能由其他保存量廉价重算的，不存；bool/int 元数据这类「反向需要的索引/掩码」通常必须存（它们不可重算且便宜）。

### 4.4 三路注册与 Python 挂载：从 TORCH_LIBRARY_IMPL 到 torch_npu.xxx

#### 4.4.1 概念说明

前两模块写好了五类函数（NPU 前向、NPU 反向、autograd 包装、Meta、Function 类），但它们目前只是躺在命名空间 `custom` 里的普通 C++ 函数。要让 Dispatcher 找得到，必须做两件事：

1. **声明 schema**（`TORCH_LIBRARY_FRAGMENT(custom, m)` + `m.def(...)`）——告诉 PyTorch「custom 命名空间里有个叫 npu_aggregate_hidden 的算子，长这个签名」。这在 [ops_def_registration.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp) 集中完成（u6-l1 已总览，本讲看它与本文件的对齐关系）。
2. **挂接实现**（`TORCH_LIBRARY_IMPL(custom, <key>, m)` + `m.impl(...)`）——把函数绑定到 (命名空间, dispatch key) 二元组上。

三路注册各拦截一种请求：`PrivateUse1` 拦「NPU 设备上的直接计算」，`AutogradPrivateUse1` 拦「需要建反向图的计算」，`Meta` 拦「meta 设备上的形状推导」。最后 `__init__.py` 在 Python 侧做一层语法糖挂载。

#### 4.4.2 核心流程

用户调用 `y = torch_npu.npu_aggregate_hidden(x, w, mask)`（x 在 NPU 上且 requires_grad=True）的完整路径：

```text
torch_npu.npu_aggregate_hidden            # __init__.py 挂载的别名
  └─ torch.ops.custom.npu_aggregate_hidden(...)   # 同一 OpOverload 对象
       └─ Dispatcher: schema = "npu_aggregate_hidden(Tensor, Tensor, *, Tensor?) -> Tensor"
            │  key 计算: 输入设备=NPU → PrivateUse1; requires_grad=True → AutogradPrivateUse1 优先
            ├─ 命中 AutogradPrivateUse1 → npu_aggregate_hidden_autograd
            │    └─ Function::apply → forward(guard) → op.call 落到 ↓
            ├─ 命中 PrivateUse1      → npu_aggregate_hidden_npu → EXEC_NPU_CMD_V1 → aclnn
            └─ (输入全在 meta 设备)  → npu_aggregate_hidden_meta → 只分配输出 shape
```

反向时：autograd 引擎调 Function::backward → `custom::npu_aggregate_hidden_grad`（只注册在 PrivateUse1 下，纯内部积木，不再套 autograd，避免「对梯度求梯度」）。

#### 4.4.3 源码精读

**schema 声明与 pybind（前提层）。**

- [ops_def_registration.cpp:44-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L44-L46)：aggregate_hidden 两条 schema——注意 `*, Tensor? mask=None` 中 `*` 之后是关键字参数、`Tensor?` 表示可选张量、返回值前者单 Tensor 后者 `(Tensor, Tensor)`。**schema 的参数个数与类型必须与 csrc 里 `m.impl` 绑定的 C++ 函数签名严格一致**，否则注册期即报错。
- [ops_def_registration.cpp:92-96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L92-L96)：`PYBIND11_MODULE` 把 `custom::npu_aggregate_hidden_autograd`（声明于 [function.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/function.h)）暴露为 Python 函数——这是 `from . import custom_ops_lib` 后能直接 `custom_ops_lib.npu_aggregate_hidden(...)` 的通道（绕过 Dispatcher 的便捷入口，一般测试用）。

**三路注册（本文件尾部）。**

- [npu_aggregate_hidden.cpp:134-138](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L134-L138)：`PrivateUse1` 下同时注册前向与反向两个算子名——反向算子也要有后端实现，供 backward 经 Dispatcher 调用。
- [npu_aggregate_hidden.cpp:140-143](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L140-L143)：`AutogradPrivateUse1` 下**只**注册前向名的 autograd 包装——`npu_aggregate_hidden_grad` 是内部积木，不对外提供二阶求导。
- [npu_aggregate_hidden.cpp:146-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L146-L149)：`Meta` 下注册前向的 shape 推导。

**Python 挂载（__init__.py）。**

- [__init__.py:9-13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L9-L13)：模块 docstring 点明目标用法：`torch.ops.custom.npu_selected_flash_attention()` 与 `torch_npu.npu_selected_flash_attention()` 两种等价调用。
- [__init__.py:17-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L17-L21)：**先** `import torch`、`import torch_npu`（确保 PrivateUse1 已被 torch_npu 认领为 NPU 后端），**再** `from . import custom_ops_lib`——import 编译好的 .so 会执行其中所有 `TORCH_LIBRARY_FRAGMENT/IMPL` 静态初始化器，schema 与实现此刻进入 Dispatcher。
- [__init__.py:25-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L25-L35)：挂载循环——取 `torch.ops.custom` 命名空间对象，`dir()` 枚举其属性（跳过 `_` 开头的内置名，如 `__name__`），把每个算子的 `OpOverload` 对象 `setattr` 到 `torch_npu` 模块上。此后 `torch_npu.npu_aggregate_hidden` 与 `torch.ops.custom.npu_aggregate_hidden` 指向同一对象。命名空间对象上能 `dir()` 出算子名，依赖 PyTorch 对已解析算子的属性缓存机制（解析发生在 import/注册之后），属实现细节，以本地运行结果为准。
- [__init__.py:37-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L37-L41)：防御性降级——找不到 `torch.ops.custom` 时只发 warning 并过滤重复告警，`torch.ops.custom.xxx` 调用方式依然可用。挂载是语法糖，不是功能依赖。

**一个特意留白的对照**：仓库已有的 [npu_sinkhorn.cpp:135-142](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp#L135-L142) 只注册了 `PrivateUse1` 与 `Meta`，**没有** Autograd 层——因为 MHC 训练脚本按 u5-l1/u5-l2 的方式显式编排 `npu_sinkhorn(out_flag=1)` 与 `npu_sinkhorn_grad`，不需要 autograd 自动焊接。这说明了三路注册是按需裁剪的：要 autograd 才挂 AutogradPrivateUse1。

#### 4.4.4 代码实践

**实践目标**：验证三路注册的存在性与 dispatch 行为差异。

**操作步骤**：

1. 在装好 wheel 的环境里执行：

   ```python
   import torch, torch_npu, omni_training_custom_ops
   # ① schema 存在性
   print(torch.ops.custom.npu_aggregate_hidden.default._schema)
   # ② 挂载结果
   print(torch_npu.npu_aggregate_hidden)
   # ③ meta 设备走 Meta 实现：只出 shape，不占 NPU
   with torch.device("meta"):
       y = torch.ops.custom.npu_aggregate_hidden(torch.randn(4, 2, 384), torch.randn(3, 384))
   print(y.shape, y.device)
   # ④ autograd 生效
   x = torch.randn(4, 2, 384, device="npu", requires_grad=True, dtype=torch.bfloat16)
   w = torch.randn(3, 384, device="npu", requires_grad=True, dtype=torch.bfloat16)
   y = torch.ops.custom.npu_aggregate_hidden(x, w)
   y.sum().backward()
   print(x.grad is not None, w.grad is not None)
   ```

2. 观察 ③ 的输出 `device=device(type='meta')`；观察 ④ 两个梯度均非 None。

**需要观察的现象**：meta 调用瞬间返回（无 NPU 计算发生，证明走的是 Meta 实现而非 NPU 实现）；requires_grad=True 时输出 y 是叶计算图的中间节点且 `y.grad_fn` 非空（证明走的是 AutogradPrivateUse1 包装）。

**预期结果**：四步全部符合预期即注册链路完好。步骤 ③④ 需要 NPU；无 NPU 环境时 ①② 也无法执行（import 依赖 torch_npu 与 .so），此时改为源码核对：在 [ops_def_registration.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp) 中数出 schema 条数、与 u6-l1 说的 18 条对照，并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TORCH_LIBRARY_IMPL` 用的是 `TORCH_LIBRARY_FRAGMENT` 配套机制，而不是一个 `TORCH_LIBRARY(custom, m)` 写完所有内容？

**答案**：`TORCH_LIBRARY` 一个命名空间只能定义一次（负责 def），而实现分散在 15 个算子的 csrc 文件里各自注册——`TORCH_LIBRARY_IMPL` 允许在不同编译单元、不同时机给同一 (namespace, key) 追加实现；schema 侧同样用 `FRAGMENT` 让多个文件共守一个命名空间。这样新增算子只需加自己的 csrc 文件，不必碰公共注册文件的实现部分（只需加一条 def）。

**练习 2**：`m.impl("npu_aggregate_hidden", &custom::npu_aggregate_hidden_autograd)` 里绑定的函数签名如果与 schema 不符（比如少一个 mask 参数），什么时候报错？

**答案**：扩展 .so 加载、注册器执行 `m.impl` 时——`impl` 内部会做 C++ 函数类型与 schema 参数的编译期/加载期匹配检查，直接抛 c10 错误，import 阶段就能看到，不会拖到运行期。

**练习 3**：`__init__.py` 为什么把挂载目标选成 `torch_npu` 而不是新建一个模块？

**答案**：训练脚本的既有习惯是 `torch_npu.npu_xxx` 调用昇腾扩展算子（如 `torch_npu.npu_flash_attention`）。挂到 torch_npu 上后，盘古训练代码可以把这些自定义算子当「原生 torch_npu 算子」用，迁移成本为零；同时 `torch.ops.custom.*` 通道保留，两不耽误。

## 5. 综合实践

**任务**：为 sinkhorn 前向算子补全带 Autograd 的 csrc 适配（仓库现有 [npu_sinkhorn.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp) 只有 NPU/Meta 两路，你来做第三路）。

已知条件（全部来自真实源码）：

- schema（[ops_def_registration.cpp:47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L47)）：`npu_sinkhorn(Tensor x, *, int out_flag=0, float eps=1e-6, int num_iters=20) -> (Tensor, Tensor, Tensor)`；
- 反向算子已就绪（[ops_def_registration.cpp:18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L18) 与 [npu_sinkhorn_grad.cpp:26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn_grad/csrc/npu_sinkhorn_grad.cpp#L26)）：`npu_sinkhorn_grad(Tensor grad_output, Tensor norm_out, Tensor sum_out) -> (Tensor)`；
- 承接 u5-l1/u5-l2：前向 `out_flag=1` 时才落盘 `norm_out`/`sum_out`，反向正是消费这两个中间量。

**示例代码（骨架，非仓库原有代码）**——核心新增内容如下（NPU 前向与 Meta 可直接复用仓库 `npu_sinkhorn.cpp` 的实现，此处略）：

```cpp
// 示例代码：sinkhorn 的 Autograd 拼接骨架
class ManifoldSinkhornFunction : public torch::autograd::Function<ManifoldSinkhornFunction> {
public:
    static variable_list forward(AutogradContext *ctx, const at::Tensor &x,
                                 int64_t out_flag, double eps, int64_t num_iters)
    {
        at::AutoDispatchBelowADInplaceOrView guard;
        static auto op = torch::Dispatcher::singleton()
                             .findSchemaOrThrow("custom::npu_sinkhorn", "")
                             .typed<decltype(npu_sinkhorn_npu)>();
        auto result = op.call(x, out_flag, eps, num_iters);   // (output, norm_out, sum_out)
        // 反向只消费 norm_out/sum_out（u5-l2），训练语义要求 out_flag == 1
        ctx->save_for_backward({std::get<1>(result), std::get<2>(result)});
        return {std::get<0>(result), std::get<1>(result), std::get<2>(result)};
    }
    static variable_list backward(AutogradContext *ctx, variable_list grad_outputs)
    {
        auto saved = ctx->get_saved_variables();
        at::Tensor grad = grad_outputs[0].defined() ? grad_outputs[0].contiguous()
                                                    : at::zeros_like(saved[0]);  // 待确认：应以 output 的 shape 补零
        static auto op = torch::Dispatcher::singleton()
                             .findSchemaOrThrow("custom::npu_sinkhorn_grad", "")
                             .typed<decltype(npu_sinkhorn_grad)>();
        auto grad_x = op.call(grad, saved[0], saved[1]);       // aclnnAiInfraSinkhornGrad
        // forward 有 4 个参数(x, out_flag, eps, num_iters)：后三个是标量，返回未定义张量
        return {grad_x, at::Tensor(), at::Tensor(), at::Tensor()};
    }
};

std::tuple<at::Tensor, at::Tensor, at::Tensor> npu_sinkhorn_autograd(
    const at::Tensor &x, int64_t out_flag, double eps, int64_t num_iters)
{
    variable_list outputs = ManifoldSinkhornFunction::apply(x, out_flag, eps, num_iters);
    return {outputs[0], outputs[1], outputs[2]};
}

TORCH_LIBRARY_IMPL(custom, AutogradPrivateUse1, m)
{
    m.impl("npu_sinkhorn", &custom::npu_sinkhorn_autograd);   // 补上第三路注册
}
```

**操作步骤**：

1. 把骨架补全成完整文件（加上头文件包含、`construct_sinkhorn_output_tensor` 的复用或引用），放入 `ops_transformer/mhc/sinkhorn/csrc/`（或独立练习目录）。
2. 核对三份契约：schema 参数个数/类型 ↔ autograd 包装函数签名 ↔ backward 返回个数（4 个）。
3. 回答两个问题并写进你的笔记：
   - **backward 里 mask（此处为 out_flag/eps/num_iters 三个标量）的梯度为什么返回空张量？** 用你自己的话把 4.3.1 的两层理由复述一遍：backward 返回个数必须与 forward 参数对齐；非浮点/标量参数不可微，返回未定义张量即告知 autograd 引擎「此路无梯度」。
   - `out_flag=0`（推理路径）时这个 Autograd 会怎样？——`norm_out`/`sum_out` 是空张量（[npu_sinkhorn.cpp:67-70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/sinkhorn/csrc/npu_sinkhorn.cpp#L67-L70)），backward 调 `npu_sinkhorn_grad` 会失败，因此可在 forward 里对 `out_flag` 做 TORCH_CHECK 拒绝。
4. （可选，有环境时）把文件加入 setup.py 的 glob 范围（u6-l1 讲过 glob 会自动收集 `ops_transformer/**/csrc/*.cpp`，通常无需改动），`bash build_and_install.sh` 重装后验证 `y = torch_npu.npu_sinkhorn(x, 1, 1e-6, 20); y[0].sum().backward()` 得到 `x.grad`。

**预期结果**：产出一份可编译意图明确的 csrc 骨架 + 一段「空梯度原因」说明。步骤 4 的运行结果待本地验证（无 NPU 环境时以编译期签名核对代替）。

## 6. 本讲小结

- `EXEC_NPU_CMD_V1` 是 csrc 与 aclnn 之间的全部桥梁：运行期 `dlopen/dlsym` 按名解析两段符号（候选顺序：自装 run 包 → vendors 优先级 → CANN 内置库），`ConvertType` 重载族完成 `at::Tensor→aclTensor`（零拷贝、optional→nullptr、标量透传），再按「第一段建 executor + 定 workspace → 分配 workspace → 第二段下发 stream」完成调用。
- 库路径全局变量在 .so 加载瞬间初始化（ops_common.cpp L13-14），因此「先装 run 包、source set_env.bash、再 import」是硬顺序；符号找不到时的报错文本来自宏内 TORCH_CHECK，可据此快速定位环境问题。
- csrc 适配三件套：NPU 前向/反向函数（预分配输出 + 按_def.cpp 声明顺序排参数）、`torch::autograd::Function` 子类（forward 加 guard 防递归并 save_for_backward，backward 处理未定义梯度后调反向算子）、Meta 实现（只推 shape）。
- backward 对 mask/标量参数返回 `at::Tensor()`（未定义张量）：一是与 forward 参数个数对齐的硬约束，二是这些参数不可微（bool 掩码是离散选择子，框架也不允许其 requires_grad），autograd 引擎把未定义张量当作 None 跳过传播。
- 三路 `TORCH_LIBRARY_IMPL` 各司其职：`PrivateUse1` 挂真实计算（含反向积木），`AutogradPrivateUse1` 只挂前向的 autograd 包装（避免对梯度再求导），`Meta` 挂形状推导；schema 集中在 `ops_def_registration.cpp` 的 `TORCH_LIBRARY_FRAGMENT`，签名与 impl 绑定必须严格一致。
- `__init__.py` 的挂载是 best-effort 语法糖：先 import torch/torch_npu，再 import custom_ops_lib 触发注册，然后把 `torch.ops.custom.*` 逐个 setattr 到 torch_npu；失败时 warning 降级，`torch.ops.custom.xxx` 永远可用。

## 7. 下一步学习建议

本讲之后，csrc（C++ 侧）的适配链路已经闭环。下一讲 **u6-l3 Python converter 与适配层测试** 讲同一目录下的 Python 侧三件套：converter 负责输入校验、默认值填充与调用 `torch.ops.custom` 接口，test 负责单算子 CPU/NPU 对比验证。建议提前浏览 [ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mhc/ai_infra_manifold_constrained_hyper_connection_post/converter/npu_ai_infra_manifold_constrained_hyper_connection_post.py) 的 converter 与 test 文件，思考：哪些职责适合放 csrc（C++），哪些适合放 converter（Python）？如果想更深理解 Dispatcher，可阅读 PyTorch 官方文档《Extending torch.func with custom ops》与 `torch/_library` 相关章节；想看更多本仓库 autograd 拼接实例，可对照阅读 `ops_transformer/attention/flash_attention_score_enhance/csrc/npu_flash_attention_score_enhance.cpp`（多输出、多可选输入的复杂版本）。
