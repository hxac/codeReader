# 综合实战：从零新增一个自定义推理算子

## 1. 本讲目标

本讲是整个学习手册的收官实战。前面五个单元分别拆解了算子的每一层：OpDef 注册（u2-l1）、aclnn 两段式接口（u2-l2）、Tiling 七步框架（u2-l3、u5-l1）、AscendC Kernel（u2-l4、u5-l2）、csrc 注册与 EXEC_NPU_CMD_V1（u3-l1、u3-l2）、UT/ST 测试（u6-l1、u6-l2）。本讲把这些碎片重新组装起来，回答一个完整的问题：

> 如果要给这个仓库新增一个自己的推理算子，从空目录到 `torch.ops.custom.npu_my_add(...)` 可以被调用，到底要写哪些文件、改哪些挂接点、补哪些测试？

学完本讲，你应该能够：

1. 独立列出「九件套」文件清单，并按正确顺序逐个编写；
2. 说清构建系统如何发现你的算子目录（GLOB 收集 → `-n` 过滤 → 挂到三个动态库目标），以及为什么目录名、def 文件名、类名三者必须严格对齐；
3. 为新算子补齐 op_host UT（无硬件验证 tiling）与 ST（真机精度对拍），并用 `build.sh -n` 走通编译链路。

本讲通篇以假想的 `my_add`（逐元素加法，输入 `x`、`y`，输出 `z`，支持 FP16/BF16）作为实战目标。所有为 `my_add` 写的代码都是**示例代码**，不是仓库原有内容；仓库真实代码以 `ai_infra_scatter_block_update`（仓库中最小的算子）为标本引用。

## 2. 前置知识

本讲假设你已完成第 1~3 单元与 u6-l1、u6-l2。用三张「认知地图」快速回顾：

**认知一：六层调用链（u3-l4）。** 一个自定义算子从 Python 到硬件经过六层边界：L1 Python 调用（`torch.ops.custom.npu_my_add`）→ L2 csrc 实现（PrivateUse1 真算 / Meta 推形状）→ L3 ops_common 适配（`EXEC_NPU_CMD_V1` 做 dlopen/dlsym 与类型转换）→ L4 aclnn 两段式接口 → L5 op_host（OpDef 查表 + Tiling 施工图）→ L6 op_kernel（`GET_TILING_DATA` 解包执行）。前三层住在 wheel 包（torch_ops_extension），后三层住在 run 包（ascendc 编译产物）。**新增算子 = 在六层的每一层各放一块正确的砖。**

**认知二：三次命名对齐（u2-l1、u3-l4）。** 同一个算子在三个世界里有三个名字，必须严格对齐：

| 世界 | 名字形态 | my_add 的样子 |
| --- | --- | --- |
| OpDef / Tiling / L0 注册 | 大驼峰类名 | `MyAdd` |
| 算子目录 / def 文件名 / kernel 入口 | 小写下划线 | `my_add` |
| torch schema / aclnn 符号 | `npu_` 前缀 + 驼峰函数名 | `npu_my_add` / `aclnnMyAdd` |

目录名 = def 文件名去掉 `_def` 后缀；类名 = 目录名的大驼峰形式。任何一处拼错，轻则编译失败，重则运行期「符号找不到」。

**认知三：测试两级验证（u6-l1、u6-l2）。** UT（`bash build.sh -u --ophost`）在纯 CPU 上用 faker 上下文驱动 tiling 逻辑，不需要 NPU；ST（pytest + `@pytest.mark.resources`）在真机上把 NPU 结果与 CPU 标杆对拍。新算子的标准验收流程是「先 UT 过、再 ST 过」。

还需要记住的构建事实（u1-l2）：`build.sh -n` 参数被翻译成 CMake 变量 `ASCEND_OP_NAME`，多个算子用分号分隔；构建最终产出三个动态库——`cust_opapi.so`（aclnn 接口）、`cust_opsproto_rt2.0.so`（算子原型）、`cust_opmaster_rt2.0.so`（tiling 实现）。

## 3. 本讲源码地图

本讲涉及的源码文件（全部来自标本算子 `ai_infra_scatter_block_update` 与构建系统）：

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt` | 算子级构建脚本：把各层源文件挂到三个动态库目标 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp` | OpDef 原型注册（九件套第 1 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h` | TilingData 定义与 tiling 类声明（第 2 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp` | tiling 计算与注册（第 3 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp` | kernel 入口（第 4 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h` | Kernel 类实现（第 5 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp` | aclnn 两段式对外接口（第 6 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.cpp` | L0 封装 `l0op::AiInfraScatterBlockUpdate`（第 7 件） |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp` | tiling UT 样板 |
| `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py` | ST 精度对拍样板 |
| `ascendc/CMakeLists.txt` | 顶层构建：发现算子目录、生成 aclnn/proto 文件名、安装与打包 |
| `ascendc/cmake/func.cmake` | `op_add_subdirectory`：GLOB 收集算子目录并按 `-n` 过滤 |
| `ascendc/build.sh` | 参数翻译与构建入口 |
| `ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp` | torch 侧算子签名集中定义处（需追加 `m.def`） |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp` | torch 侧实现样板（需仿写） |
| `ascendc/torch_ops_extension/setup.py` | wheel 打包：两条 glob 自动收集 csrc |

## 4. 核心概念与源码讲解

### 4.1 全流程开发：九件套文件清单与编写顺序

#### 4.1.1 概念说明

「九件套」是本手册对新增一个 AscendC 推理算子所需最小文件集合的称呼：

| # | 文件 | 所属层 | 职责 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | `op_host/my_add_def.cpp` | op_host | OpDef 原型：输入/输出/属性/SOC 声明 | 必选 |
| 2 | `op_host/my_add_tiling.h` | op_host | `TilingData` 字段定义 + tiling 类声明 | 必选 |
| 3 | `op_host/my_add_tiling.cpp` | op_host | tiling 计算 + 注册到框架 | 必选 |
| 4 | `op_host/my_add_infershape.cpp` | op_host | 图模式下的输出形状推导 | 可选（aclnn 直调不需要） |
| 5 | `op_kernel/my_add.cpp` | op_kernel | kernel 入口函数 | 必选 |
| 6 | `op_kernel/my_add.h` | op_kernel | Kernel 类（Init/Process） | 必选 |
| 7 | `op_api/aclnn_my_add.cpp` | op_api | 对外两段式接口 + 参数检查 | 必选（自己实现 aclnn 时） |
| 8 | `op_api/my_add.cpp` | op_api | L0 封装（`l0op::MyAdd` + `ADD_TO_LAUNCHER_LIST_AICORE`） | 与第 7 件配对 |
| 9 | `CMakeLists.txt`（算子目录根） | 构建 | 把第 1/3/7/8 件挂到构建目标 | 必选 |

配套头文件（`aclnn_my_add.h`、`my_add.h`）与 `docs/`、`tests/` 不计入件数。torch 侧的两个动作（在 `ops_def_registration.cpp` 追加 `m.def`、新建 csrc 实现文件）在第 5 节综合实践中完成。

为什么要这个顺序？因为文件之间存在单向依赖：**def 先行**（aclnn 层的 `CREATE_EXECUTOR` 要查 OpDef 表，tiling 注册的键也是 OpDef 类名）→ **tiling.h 与 kernel 同步设计**（`TilingData` 是 host/device 两端的序列化契约，字段增减必须双侧同步）→ **L0 封装**（aclnn 层调用的就是它）→ **aclnn 接口**（组装检查与执行器）→ **CMakeLists**（随时可以写，但放最后正好核对清单）。建议每写完一件就对照本表打勾。

#### 4.1.2 核心流程

新增 `my_add` 的推荐执行顺序（伪代码）：

```text
1. 建目录 src/ops-transformer/index/my_add/{op_host,op_kernel,op_api,tests/{ut/op_host,st}}
2. 写 def.cpp        → 声明 x/y/z 三个张量、FP16/BF16、AddConfig("ascend910_93")
3. 写 tiling.h       → TilingData（totalCoreNum/usedCoreNum/eachCoreElemCount/...）
4. 写 tiling.cpp     → 继承 TilingBaseClass 七步 + IMPL_OP_OPTILING(MyAdd)
5. 写 kernel.h/.cpp  → Kernel<MyType> 类 + extern "C" 入口 + TILING_KEY_IS
6. 写 L0 封装        → OP_TYPE_REGISTER(MyAdd) + ADD_TO_LAUNCHER_LIST_AICORE
7. 写 aclnn 两段式    → 三层检查 + CommonProcess + GetWorkspaceSize/执行段
8. 写 CMakeLists.txt → 挂 op_host_aclnnInner / optiling / opapi
9. bash build.sh -n 'my_add' 验证编译
10. torch 侧：ops_def_registration.cpp 加 m.def + 新建 csrc/my_add.cpp
11. UT（tiling 用例）→ ST（NPU/CPU 对拍）→ 真机调用
```

其中第 3~5 步是一个「三角」：TilingData 字段由 tiling 计算、由 kernel 消费，写任何一侧时都要想着另外两侧。

#### 4.1.3 源码精读

**第 1 件：def.cpp。** 标本的核心结构——类名 `AiInfraScatterBlockUpdate` 继承 `OpDef`，构造函数里流式声明输入输出：

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:L18-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L18-L29)：定义 `AiInfraScatterBlockUpdate : public OpDef`，第一个输入 `input` 声明 `ParamType(REQUIRED)`、8 个 dtype/格式组合（`DT_BF16/DT_FLOAT16/DT_FLOAT/DT_INT8` 各两列对应 indices 的 INT32/INT64，按下标对齐成组合表，u2-l1 讲过的机制）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:L56-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L56-L63)：`OpAICoreConfig` 打开动态 shape/格式/rank 能力开关，`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 把算子登记到两个 SOC。`my_add` 只需把 dtype 列表换成 FP16/BF16、SOC 按目标芯片保留。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp:L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp#L67)：`OP_ADD(AiInfraScatterBlockUpdate)` —— 编译进 `cust_opsproto_rt2.0.so` 后由静态全局对象在加载期注册进全局表。`my_add` 对应写 `OP_ADD(MyAdd)`。

**第 2、3 件：tiling 两件。** `TilingData` 是 host/device 契约：

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h:L25-L43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L25-L43)：`BEGIN_TILING_DATA_DEF ... END_TILING_DATA_DEF` 定义 14 个字段（总索引数、分核参数、单行大小、stride 等），随后 `REGISTER_TILING_DATA_CLASS(AiInfraScatterBlockUpdate, ...)` 以 OpDef 类名为键注册——这就是「类名是 def 与 tiling 的关联键」的落点。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h:L50-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L50-L84)：tiling 类继承公共基类 `TilingBaseClass` 并 override 七个虚函数（`GetPlatformInfo`/`GetShapeAttrsInfo`/`DoOpTiling`/`GetTilingKey`/`GetWorkspaceSize`/`PostTiling` 等），注释里标了 1~7 步。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:L279-L353](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L279-L353)：`DoOpTiling` 的主体——取形状校验后，先算单行字节数与对齐（L303-305），再按「均分 + 尾核兜余数」分核（L309-313：`eachCoreIndexCount_ = CeilDiv(total, cores)`、`tailCoreIndexCount_ = total - each * (used - 1)`），再按 UB 预算与双缓冲算每次搬运行数（L317-341），最后 `tilingKey_ = FULL_LOAD_TILING_KEY`（L350）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:L376-L416](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L376-L416)：`PostTiling` 把全部计算结果 `set_*` 进 TilingData（L379-393），`SaveToBuffer` 序列化进框架（L403），`SetBlockDim(usedCoreNum_)` 设启动核数（L407），登记 workspace（L410-413）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp:L452-L454](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L452-L454)：`IMPL_OP_OPTILING(AiInfraScatterBlockUpdate).Tiling(...).TilingParse<...>(...)` 把 tiling 函数挂到框架——没有这三行，算子有 TilingData 也不会被调用。`my_add` 把类名整体替换即可。

**第 5、6 件：kernel 两件。**

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp:L25-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L25-L35)：kernel 入口。三个要点：`extern "C" __global__ __aicore__` 修饰（L25）；参数布局按 OpDef 的 IO 顺序 + 末尾 `workspace`、`tiling`（L26）；`GET_TILING_DATA` 解包（L28）+ `TILING_KEY_IS(FULL_LOAD_TILING_KEY)` 分支（L30，注意 L23 的宏值 1000 与 tiling 侧 L59 的 `FULL_LOAD_TILING_KEY = 1000` 是硬编码镜像，两侧必须一致）。`DTYPE_INPUT`/`DTYPE_INDICES` 是编译系统按 OpDef 组合注入的类型宏。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h:L62-L94](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L62-L94)：`Init` 三步——`SetGlobalBuffer` 绑定 GM（L69-71）、读 tiling 字段（L74-84）、`pipe_->InitBuffer` 划拨 UB（L88-93）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h:L96-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L96-L120)：`Process` 用 `GetBlockIdx()` 领区间（尾核用 `tailCoreIndexCount_` 兜底，L104-105），按 `maxIndicesPerLoad_` 分批 `CopyIn → ScatterOut`（L117-120）。`my_add` 把「按索引散写」换成「逐元素 x+y→z」的向量计算即可，骨架完全复用。

**第 7、8 件：aclnn 两件套。**

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.cpp:L23-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/ai_infra_scatter_block_update.cpp#L23-L36)：L0 封装全文只有三件事——`OP_TYPE_REGISTER(AiInfraScatterBlockUpdate)`（L23）、`ADD_TO_LAUNCHER_LIST_AICORE` 把算子登记进 executor 下发列表（L30-31，注意 `OP_OUTPUT(input)` 说明这是原地算子）、返回输出张量（L35）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp:L42-L103](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L42-L103)：参数三步检查——`NotNull`（L42-57，先判空才能解引用）、空张量（L59-74）、dtype 合法 + input/update 一致（L76-89），由 `CheckAiInfraScatterBlockUpdateParams` 按固定次序串起来（L91-103）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp:L139-L161](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.cpp#L139-L161)：两段式接口本体——第一段 `GetWorkspaceSize` 里 `CREATE_EXECUTOR()`（L145）、走 `CommonProcess`（L148，内部 L133 调 `l0op::AiInfraScatterBlockUpdate` 完成登记）、回填 workspace 并交出 executor（L151-152）；第二段只有一行 `CommonOpExecutorRun`（L160）。`my_add` 是非原地算子，CommonProcess 里不需要 `CreateView`，直接对 x/y 用 `l0op::Contiguous` 即可。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h:L37-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_api/aclnn_ai_infra_scatter_block_update.h#L37-L52)：两段式函数的 extern "C" 声明，csrc 层 dlsym 找的符号名就是这里的函数名 `aclnnMyAddGetWorkspaceSize` / `aclnnMyAdd`。

#### 4.1.4 代码实践

**实践：写出 my_add 的 def 与 kernel 入口骨架（纯阅读型，无需硬件）。**

1. 实践目标：亲手产出九件套中最关键的两件——`my_add_def.cpp` 与 `op_kernel/my_add.cpp`，检验对命名对齐和参数布局的掌握。
2. 操作步骤：
   - 在纸上或本地新建 `my_add_def.cpp`，以下为**示例代码**（非仓库原有）：

     ```cpp
     #include "register/op_def_registry.h"
     namespace ops {
     class MyAdd : public OpDef {
     public:
         explicit MyAdd(const char* name) : OpDef(name)
         {
             this->Input("x").ParamType(REQUIRED)
                 .DataType({ge::DT_FLOAT16, ge::DT_BF16})
                 .Format({ge::FORMAT_ND, ge::FORMAT_ND});
             this->Input("y").ParamType(REQUIRED)
                 .DataType({ge::DT_FLOAT16, ge::DT_BF16})
                 .Format({ge::FORMAT_ND, ge::FORMAT_ND});
             this->Output("z").ParamType(REQUIRED)
                 .DataType({ge::DT_FLOAT16, ge::DT_BF16})
                 .Format({ge::FORMAT_ND, ge::FORMAT_ND});
             OpAICoreConfig aicConfig;
             aicConfig.DynamicCompileStaticFlag(true).DynamicShapeSupportFlag(true);
             this->AICore().AddConfig("ascend910_93", aicConfig);
         }
     };
     OP_ADD(MyAdd);
     }
     ```

   - 再写 `op_kernel/my_add.cpp` 入口（**示例代码**）：

     ```cpp
     #include "kernel_operator.h"
     #include "my_add.h"
     using namespace AscendC;
     #define MY_ADD_TILING_KEY 1000

     extern "C" __global__ __aicore__ void my_add(
         GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling)
     {
         GET_TILING_DATA(tilingData, tiling);
         TPipe pipe;
         if (TILING_KEY_IS(MY_ADD_TILING_KEY)) {
             MyAddKernel<DTYPE_X> op;   // DTYPE_X 由构建系统按 OpDef 注入
             op.Init(x, y, z, tilingData, &pipe);
             op.Process();
         }
     }
     ```

3. 需要观察的现象：对照 4.1.3 引用的标本代码，逐行核对——类名/目录名/`OP_ADD` 参数是否满足「大驼峰 ↔ 小写下划线」对应；kernel 入口参数顺序是否与 def 的 `x/y/z` 声明顺序一致、末尾是否追加 `workspace` 与 `tiling`。
4. 预期结果：两份骨架能够与标本逐条对上；如果你发现自己在 def 里声明了 `z` 却在 kernel 参数里漏写，正是本实践要暴露的典型错误。
5. 本实践不涉及运行，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1：** `my_add` 需要像标本那样为 `input` 支持 `CreateView` 的非连续原地写入吗？
**答案：** 不需要。`CreateView` 是为「原地写目标」保留 stride/offset 的手段（标本 input 既是输入又是输出）；`my_add` 输出到独立的 `z`，x/y 只读，走 `l0op::Contiguous` 连续化即可。

**练习 2：** 如果把 `my_add` 的 kernel 入口函数名写成 `MyAdd`（大驼峰），会发生什么？
**答案：** 构建系统按 OpDef 的小写下划线算子名生成二进制编译配置，入口名不匹配会导致链接/符号查找失败； AscendC kernel 入口约定与目录同名的小写下划线形式（对照标本 `ai_infra_scatter_block_update`）。

**练习 3：** `FULL_LOAD_TILING_KEY` 在 tiling.cpp 是 1000、kernel 里写成 1001，编译能过吗？运行会怎样？
**答案：** 能编译——这是两个独立编译单元里的普通常量，编译器无法发现不一致。运行时 host 设 key=1000，device 侧 `TILING_KEY_IS(1001)` 不命中，kernel 静默空跑、输出错误。这正是 u2-l4 强调「key 双侧硬编码镜像、靠 UT 断言兜底」的原因——UT 的 `expectTilingKey` 断言可以兜住 host 侧。

### 4.2 构建挂接：从目录被发现到进入三个动态库

#### 4.2.1 概念说明

写完九件套只是「砖齐了」，还必须让构建系统**发现**并**归位**这些砖。本模块回答四个问题：

1. 构建系统怎么知道多了一个算子目录？——不需要注册，靠 GLOB 递归收集。
2. `build.sh -n 'my_add'` 怎么生效？——`ASCEND_OP_NAME` 变量在收集阶段过滤。
3. 源文件怎么进三个动态库？——算子级 `CMakeLists.txt` 用 `target_sources` 挂到 `op_host_aclnnInner`/`optiling`/`opapi`。
4. def 文件名如何驱动代码生成？——顶层 CMake 用正则 `_def$` 推导自动生成的 aclnn/proto 文件名。

理解这条链，你才能解释「为什么目录放错一层就编译不到」「为什么改了 def 没改 CMake 会漏编」。

#### 4.2.2 核心流程

```text
build.sh -n 'my_add' -c ascend910_93
  └─ ascend_op_name="my_add" ──翻译──> -DASCEND_OP_NAME=my_add
       └─ 顶层 CMakeLists: op_add_subdirectory(OP_LIST OP_DIR_LIST)
            └─ func.cmake: file(GLOB src/ops-transformer/**/**/CMakeLists.txt)
                 └─ 逐目录取 OP_NAME，不在 ASCEND_OP_NAME 列表则 continue（过滤）
                 └─ 命中 my_add → OP_DIR_LIST += <...>/index/my_add
       └─ foreach OP_DIR: add_subdirectory(<op_dir>)
            └─ 算子 CMakeLists.txt 执行：
                 def.cpp      → target_sources(op_host_aclnnInner)   ┐ 进 opsproto 链
                 tiling.cpp   → target_sources(optiling)             ┐ 进 cust_opmaster_rt2.0.so
                 aclnn + L0   → target_sources(opapi)                ┐ 进 cust_opapi.so
                 kernel 文件  → install 到 vendors/.../ascendc/my_add ┐ 供 opc 编译二进制
       └─ 顶层按 "_def$" 正则生成 autogen 的 aclnn_my_add.* / my_add_proto.* 文件名
```

注意 GLOB 模式是 `src/ops-transformer/**/**/CMakeLists.txt`——即 `<族>/<算子名>/CMakeLists.txt` 两层结构。`my_add` 放在 `src/ops-transformer/index/my_add/` 会被发现；若直接放在 `src/ops-transformer/my_add/`（少一层）则永远不会被编译。

#### 4.2.3 源码精读

**发现与过滤：**

- [ascendc/build.sh:L268-L271](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L268-L271)：`-n|--op-name` 参数解析，值存入 `ascend_op_name`。
- [ascendc/build.sh:L362-L364](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L362-L364)：非空时拼入 `CUSTOM_OPTION ... -DASCEND_OP_NAME=${ascend_op_name}`——build.sh 只是参数翻译器（u1-l2 的结论）。
- [ascendc/cmake/func.cmake:L41-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L41-L45)：`op_add_subdirectory` 函数开头，`file(GLOB ...)` 按两层模式收集所有算子级 CMakeLists。
- [ascendc/cmake/func.cmake:L62-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L62-L68)：过滤逻辑——`ASCEND_OP_NAME` 非 all 时，`OP_NAME` 不在列表则 `continue()`。这就是「只编译 my_add」的实现位置。
- [ascendc/CMakeLists.txt:L300-L305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L300-L305)：顶层先 `add_subdirectory` 公共库，再调 `op_add_subdirectory(OP_LIST OP_DIR_LIST)` 收集算子。
- [ascendc/CMakeLists.txt:L343-L352](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L343-L352)：对收集到的每个算子目录 `add_subdirectory`，此时算子自己的 CMakeLists.txt 才被执行。

**算子级挂接（第 9 件 CMakeLists.txt）：**

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:L9-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L9-L16)：文件头注释点明关键区分——`op_host_aclnnInner` 用于**自己实现了 aclnn 接口**的算子（本仓库全部如此），`op_host_aclnn` 用于使用自动生成接口的算子；`add_ops_compile_options(OP_NAME AiInfraScatterBlockUpdate ...)` 声明算子名与编译选项。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:L19-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L19-L25)：`def.cpp` 挂到 `op_host_aclnnInner`；`tiling.cpp` 挂到 `optiling`（并在非 `BUILD_OPEN_PROJECT` 时同时挂到 `opmaster_ct`）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:L33-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L33-L45)：`optiling` 的 include 目录指向本算子 `op_host`；`aclnn_ai_infra_scatter_block_update.cpp` 与 L0 封装 `ai_infra_scatter_block_update.cpp` 一起挂到 `opapi` 目标；aclnn 头文件安装到统一的 include 目录。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt:L47-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/CMakeLists.txt#L47-L55)：递归进入子目录（`tests` 在未开 `ENABLE_TEST` 时被剔除）——这就是 UT 目录自动挂接的机制，新增 `tests/ut/op_host/CMakeLists.txt` 无需改父级。

**三个目标与产物（顶层）：**

- [ascendc/CMakeLists.txt:L108-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L108-L155)：`opapi` 共享库定义、链接 CANN 基础库，`OUTPUT_NAME cust_opapi`，安装到 `packages/vendors/${VENDOR_NAME}/op_api/lib`。
- [ascendc/CMakeLists.txt:L158-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L158-L201)：`opsproto` 共享库，`OUTPUT_NAME cust_opsproto_rt2.0`——def 的最终归宿。
- [ascendc/CMakeLists.txt:L204-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L204-L257)：`optiling` 共享库，`OUTPUT_NAME cust_opmaster_rt2.0`——tiling 的最终归宿。
- [ascendc/CMakeLists.txt:L386-L398](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L386-L398)：遍历收集到的 def 源文件，用 `string(REGEX REPLACE "_def$" "" _op_name ...)` 从文件名推导算子名，进而拼出 autogen 的 `aclnn_${_op_name}.cpp/.h` 与 `${_op_name}_proto.cpp/.h` 文件名——**def 文件名因此成为契约**：`my_add_def.cpp` 会驱动生成 `aclnn_my_add.*` 与 `my_add_proto.*`。
- [ascendc/CMakeLists.txt:L665-L698](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/CMakeLists.txt#L665-L698)：kernel 源文件按算子目录 GLOB 后安装到 `${IMPL_INSTALL_DIR}/ascendc/${_op_name}`，供 opc 离线编译成芯片二进制——kernel 不进动态库，而是以源码形态随 run 包分发。

**torch 侧挂接：**

- [ascendc/torch_ops_extension/setup.py:L49-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L56)：两条 glob——`csrc_base/*.cpp`（含 ops_def_registration.cpp）与 `omni_custom_ops/*/*/*/csrc/*.cpp`（四层：包/族/算子/csrc）。把 `my_add` 的 csrc 放到 `omni_custom_ops/ops_transformer/index/my_add/csrc/my_add.cpp` 就会被自动收进 `custom_ops_lib`，**无需改打包脚本**（u1-l4 的结论在此落地）。

#### 4.2.4 代码实践

**实践：写出 my_add 的算子级 CMakeLists.txt 并静态验证挂接。**

1. 实践目标：产出第 9 件，并验证构建系统能发现它。
2. 操作步骤：
   - 以标本为模板写 `src/ops-transformer/index/my_add/CMakeLists.txt`（**示例代码**）：

     ```cmake
     add_ops_compile_options(
             OP_NAME MyAdd
             OPTIONS --cce-auto-sync=off -Wno-deprecated-declarations -Werror
     )
     target_sources(op_host_aclnnInner PRIVATE
             op_host/my_add_def.cpp
     )
     target_sources(optiling PRIVATE
             op_host/my_add_tiling.cpp
     )
     target_include_directories(optiling PRIVATE
             ${CMAKE_CURRENT_SOURCE_DIR}/op_host
     )
     target_sources(opapi PRIVATE
             op_api/aclnn_my_add.cpp
             op_api/my_add.cpp
     )
     install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/op_api/aclnn_my_add.h
             DESTINATION ${ACLNN_INC_INSTALL_DIR} OPTIONAL
     )
     ```

   - 有昇腾环境时执行 `bash build.sh -n 'my_add' -c ascend910_93`，观察 CMake 配置期输出中 `my_add` 目录是否被加入编译列表；无环境时做静态检查：核对目录层级是 `src/ops-transformer/index/my_add/`（两层，能被 func.cmake 的 GLOB 命中）、`OP_NAME MyAdd` 与 def 类名一致。
3. 需要观察的现象：配置阶段日志中出现算子目录；编译阶段三个目标分别编过 `my_add` 的对应源文件（或在没有硬件/完整 CANN 的环境下，配置阶段即报出缺依赖——这本身也验证了「目录已被发现」）。
4. 预期结果：`build.sh -n 'my_add'` 只编译 my_add 一个算子（对照全量编译耗时差异）。
5. 编译产物路径与是否成功需在昇腾环境验证，无环境时以静态检查为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1：** 新增算子后忘了把 `my_add_tiling.cpp` 加进 CMakeLists 的 `target_sources(optiling ...)`，症状是什么？
**答案：** 配置与编译都能过（optiling 库里只是少了这个目标文件），但运行期框架查 `MyAdd` 的 tiling 实现时找不到 `IMPL_OP_OPTILING` 注册，tiling 报「算子不支持」类错误。这是「编译静默、运行爆炸」的典型挂接遗漏。

**练习 2：** 为什么 `my_add` 的 def 文件必须叫 `my_add_def.cpp`，而不能叫 `myadd_def.cpp` 或 `my_add_definition.cpp`？
**答案：** 有两重约束：目录名（GLOB 收集后作为 OP_NAME 与 `-n` 匹配）必须等于 def 文件名去掉 `_def$` 后缀（顶层 CMakeLists 用正则推导 autogen 文件名）；同时 OpDef 类名约定为目录名的大驼峰。改名会同时破坏 `-n my_add` 过滤与 `aclnn_my_add.*` 生成。

**练习 3：** `-n` 一次编译多个算子怎么写？
**答案：** 用分号分隔，如 `bash build.sh -n 'my_add;ai_infra_scatter_block_update'`。依据是 build.sh 的 `check_ophost_test_exists` 用 `IFS=';' read -ra op_names` 拆分（[ascendc/build.sh:L232-L243](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L232-L243)），CMake 侧 `ASCEND_OP_NAME` 也是列表语义（func.cmake 的 `IN_LIST` 判断）。

### 4.3 测试补齐：UT 与 ST 的最小闭环

#### 4.3.1 概念说明

新算子没有测试等于裸奔。本仓库的两级测试各管一段：

- **UT（单元测试）**：在纯 CPU 上用 faker 上下文驱动 op_host 的 tiling/infershape 逻辑（u6-l1）。对新增算子的价值：**TilingData 字段算得对不对、异常输入拦不拦得住**，不需要 NPU 就能验证——这恰好兜住了 4.1.5 练习 3 那类「host 侧 key 断言」问题。
- **ST（系统测试）**：真机上把 NPU 结果与 CPU 标杆对拍（u6-l2）。对新增算子的价值：**端到端六层链路全通、数值正确**。

两者挂接都是「放对目录 + 命名跟随」：UT 放 `tests/ut/op_host/`、ST 放 `tests/st/`，目录由算子 CMakeLists 的递归逻辑自动进入（见 4.2.3 第 4 条引用）。

#### 4.3.2 核心流程

UT 用例的标准形态：

```text
1. 构造 CompileInfo（核数/UB 大小）
2. 构造 gert::TilingContextPara("MyAdd", {输入描述列表}, {输出描述列表}, {attrs}, &compileInfo)
   —— 每个张量描述是 {{viewShape, storageShape}, dtype, format}
3. 声明期望：expectTilingKey / expectWorkspaces
4. ExecuteTestCase(para, 期望返回值, expectTilingKey, expectTilingDataStr, expectWorkspaces, ...)
   —— 框架查注册表直调 tiling 函数并断言
```

ST 用例的标准形态：

```text
1. import omni_custom_ops（不可省，挂载靠 import 副作用）
2. 写 golden 函数（CPU 标杆）
3. 构造随机输入 + torch.manual_seed 固定种子
4. 输入 .npu() 上设备，调用 torch.ops.custom.npu_my_add
5. 与 CPU 标杆按容差或二进制一致断言；失败信息带 shape/dtype/max_diff
6. 每个测试方法贴 @pytest.mark.resources(device="npu:*", npus_per_node=1)
```

#### 4.3.3 源码精读

**UT 样板：**

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp:L59-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L59-L89)：一个正常用例的完整形态——`TilingContextPara` 以 OpDef 类名字符串 `"AiInfraScatterBlockUpdate"` 为键（L67），三个输入与一个输出的 `{{shape, shape}, dtype, FORMAT_ND}` 描述（L68-79），空 attrs，`&compileInfo` 传入平台信息（L81）；随后 `ExecuteTestCase(..., ge::GRAPH_SUCCESS, expectTilingKey=1000, ..., expectWorkspaces={0}, ...)`（L84-88）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp:L185-L210](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/test_ai_infra_scatter_block_update_tiling.cpp#L185-L210)：异常用例形态——故意传 2D input（L196），期望 `ge::GRAPH_FAILED`（L209）。异常用例与正常用例同构，只改输入与期望值，这正是补用例成本极低的原因。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/CMakeLists.txt:L10-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/ut/op_host/CMakeLists.txt#L10-L17)：`add_modules_ut_sources(UT_NAME ${OP_TILING_MODULE_NAME} MODE PRIVATE DIR ...)` 把本目录源文件挂进 UT 目标——新建 `tests/ut/op_host/` 放入用例并复制这份 CMakeLists 即完成挂接。

**ST 样板：**

- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py:L16-L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L16-L33)：CPU 标杆 `golden_scatter_block_update`——逐条索引循环赋值，语义即算子文档公式。`my_add` 的标杆就是 `input + update` 一行。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py:L36-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L36-L83)：一次完整对拍——固定种子（L39）、构造输入与标杆（L60）、上设备并调用原地版本（L63-71）、`torch.equal` 二进制一致比较（L75）、失败信息带 shape/dtype/max_diff/mismatch 计数（L76-83）。
- [ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py:L87-L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py#L87-L92)：测试类与 `@pytest.mark.resources(device="npu:*", npus_per_node=1)` marker——忘贴会被 conftest 静默 deselect（u6-l2 的教训）。

**触发方式：**

- [ascendc/build.sh:L292-L294](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L292-L294)：`-u|--test` 置 `ENABLE_TEST=TRUE`。
- [ascendc/build.sh:L109-L121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L109-L121)：`build_ut` 按 `OP_HOST_UT`/`OP_API_UT` 分派构建 `transformer_op_host_ut` 等目标。组合命令：`bash build.sh -u --ophost -n 'my_add'`（`--ophost` 在 [ascendc/build.sh:L338](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/build.sh#L338) 解析）。

#### 4.3.4 代码实践

**实践：为 my_add 写一个 tiling UT 用例（无硬件可写、可评审）。**

1. 实践目标：仿照样板产出 `tests/ut/op_host/test_my_add_tiling.cpp` 的第一条用例。
2. 操作步骤（**示例代码**）：

   ```cpp
   #include <iostream>
   #include <gtest/gtest.h>
   #include "../../../op_host/my_add_tiling.h"
   #include "tiling_context_faker.h"
   #include "tiling_case_executor.h"

   class MyAddTiling : public testing::Test {};

   TEST_F(MyAddTiling, MyAddTiling_Normal_fp16)
   {
       optiling::MyAddCompileInfo compileInfo = {};
       gert::TilingContextPara tilingContextPara("MyAdd",
           {
               // x: shape (1024, 4096), FP16（描述结构为 {{viewShape, storageShape}, dtype, format}）
               {{{1024, 4096}, {1024, 4096}}, ge::DT_FLOAT16, ge::FORMAT_ND},
               // y: shape (1024, 4096), FP16
               {{{1024, 4096}, {1024, 4096}}, ge::DT_FLOAT16, ge::FORMAT_ND},
           },
           {
               // z: shape (1024, 4096), FP16
               {{{1024, 4096}, {1024, 4096}}, ge::DT_FLOAT16, ge::FORMAT_ND},
           },
           {}, &compileInfo);

       ExecuteTestCase(tilingContextPara, ge::GRAPH_SUCCESS, 1000,
                       "", {0}, 0, nullptr);
   }
   ```

   写完后对照样板 L59-89 逐项检查：类名字符串、输入个数与顺序是否与 def 一致、期望 key 与 kernel 侧 `TILING_KEY_IS` 的值是否同为 1000。
3. 需要观察的现象：有环境时 `bash build.sh -u --ophost -n 'my_add'` 编出 UT 可执行文件并跑通；无环境时请同伴/自己按上述三条静态评审。
4. 预期结果：用例通过表明 tiling 对该 shape 计算成功且 key=1000；把期望 key 改成 1001 应当失败——这验证了断言真的在生效。
5. 运行结果待本地验证（无昇腾环境时以静态评审为准）。

#### 4.3.5 小练习与答案

**练习 1：** `my_add` 的 ST 应该用 `torch.equal` 还是 `torch.allclose`？容差怎么定？
**答案：** 逐元素加法在 fp32 下可用较紧容差，fp16/bf16 下因舍入建议 `torch.allclose(rtol=1e-2, atol=1e-2)` 量级（对齐仓库 bf16 用 1e-2 的惯例，u6-l2）；若把标杆也写成同 dtype的 CPU 加法且硬件走全精度通路，可退化为二进制一致比较。原则：**纯搬运用二进制、浮点算术用容差**。

**练习 2：** 为什么 ST 文件顶部 `import omni_custom_ops` 不能省？
**答案：** csrc 的 `TORCH_LIBRARY_FRAGMENT`/`TORCH_LIBRARY_IMPL` 注册与 torch_npu 镜像挂载全部靠该包被 import 时的副作用完成（u1-l4）；省略后 `torch.ops.custom.npu_my_add` 不存在，用例直接报 schema 找不到。

**练习 3：** ST 里只写了一个 dtype 的用例就提交了，风险是什么？
**答案：** def 声明了 FP16/BF16 两个 dtype 组合，每个组合会实例化独立的 kernel 二进制（`DTYPE_X` 编译期注入）；只测一个 dtype，另一个组合的 kernel 路径完全未验证。测试矩阵应覆盖 def 的全部 dtype 组合与边界 shape（对齐样板按 dtype/对齐性/边界分组的做法）。

## 5. 综合实践

**综合实战：把 my_add 从零带到 torch 可调用。** 这是本讲的总装任务，整合 4.1/4.2/4.3 的全部产出。

**任务清单（建议按序打勾）：**

| 阶段 | 动作 | 产出/验证 |
| --- | --- | --- |
| A1 | 建目录 `src/ops-transformer/index/my_add/{op_host,op_kernel,op_api,tests/ut/op_host,tests/st}` | 目录两层结构能被 GLOB 命中 |
| A2 | 写 `op_host/my_add_def.cpp`（4.1.4 骨架） | `OP_ADD(MyAdd)` |
| A3 | 写 `op_host/my_add_tiling.h/.cpp`：TilingData 至少含 `totalElemCount/usedCoreNum/eachCoreElemCount/tailCoreElemCount/elemPerLoad`，继承 `TilingBaseClass` 实现七步，末尾 `IMPL_OP_OPTILING(MyAdd).Tiling(...).TilingParse<MyAddCompileInfo>(...)` | 注册键 = 类名 |
| A4 | 写 `op_kernel/my_add.h/.cpp`：Kernel 类读 tiling、`TQue` 双缓冲 CopyIn 两个输入、`AscendC` 向量加 `Compute(x+y)`、CopyOut 到 z；入口 `TILING_KEY_IS(1000)` | key 与 A3 一致 |
| A5 | 写 `op_api/my_add.cpp`（L0：`OP_TYPE_REGISTER(MyAdd)` + `ADD_TO_LAUNCHER_LIST_AICORE(MyAdd, OP_INPUT(x,y), OP_OUTPUT(z))`） | 输出改为 z（非原地） |
| A6 | 写 `op_api/aclnn_my_add.cpp/.h`：三层检查（NotNull/Empty/Dtype：x 与 y 同 dtype 且在 FP16/BF16 列表）+ CommonProcess（x/y 走 `l0op::Contiguous`，调 `l0op::MyAdd`）+ 两段式接口 | 符号名 `aclnnMyAdd*` |
| A7 | 写算子 `CMakeLists.txt`（4.2.4 模板） | 三目标挂接 |
| A8 | `bash build.sh -n 'my_add' -c ascend910_93` | 编译链路通（无硬件时以配置阶段发现目录为准） |
| B1 | 在 `torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp` 追加（**示例代码**）：`m.def("npu_my_add(Tensor x, Tensor y) -> Tensor");` | 追加在 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:L16-L165](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L16-L165) 的 `TORCH_LIBRARY_FRAGMENT` 块内（对照 L107-108 scatter 的两条写法） |
| B2 | 新建 `omni_custom_ops/ops_transformer/index/my_add/csrc/my_add.cpp`（**示例代码**）： | setup.py glob 自动收集 |

B2 的参考骨架（对照 [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp:L19-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp#L19-L58) 的四段式：真算实现 → Meta 实现 → PrivateUse1 注册 → Meta 注册）：

```cpp
#include <torch/library.h>
#include "../../../../csrc_base/ops_common.h"

namespace custom {
using namespace at_npu::native;

at::Tensor npu_my_add(const at::Tensor &x, const at::Tensor &y)
{
    at::Tensor out = at::empty_like(x);
    EXEC_NPU_CMD_V1(aclnnMyAdd, x, y, out);
    return out;
}

at::Tensor npu_my_add_meta(const at::Tensor &x, const at::Tensor &y)
{
    return at::empty(x.sizes(), x.options());
}
} // namespace custom

TORCH_LIBRARY_IMPL(custom, PrivateUse1, m) {
    m.impl("npu_my_add", &custom::npu_my_add);
}
TORCH_LIBRARY_IMPL(custom, Meta, m) {
    m.impl("npu_my_add", &custom::npu_my_add_meta);
}
```

| 阶段 | 动作 | 产出/验证 |
| --- | --- | --- |
| B3 | 重打 wheel 并安装（u1-l4 的 `build_and_install.sh`），先装 run 包再装 wheel | 顺序不能反（u3-l4） |
| C1 | 补 UT：`tests/ut/op_host/test_my_add_tiling.cpp`（4.3.4 已给出首条）+ 复制样板的 `tests/ut/op_host/CMakeLists.txt`；`bash build.sh -u --ophost -n 'my_add'` | UT 绿 |
| C2 | 补 ST：`tests/st/test_my_add.py`（golden = CPU 加法、FP16/BF16 两组、边界 shape、`@pytest.mark.resources` marker）；`pytest <算子st目录> <框架st目录>` 执行（须一并传框架目录以加载 conftest，u6-l2） | ST 绿 |
| C3 | 端到端冒烟：`import omni_custom_ops; z = torch.ops.custom.npu_my_add(x.npu(), y.npu())`；再验证 `torch_npu.npu_my_add` 等价写法 | 六层链路全通 |

**验收标准：** A8 编译通过、C1 UT 通过、C2 ST 与 CPU 标杆在容差内一致、C3 两种调用写法结果一致。无硬件环境下，A/B 阶段以「编译系统静态检查 + 代码评审」为验收基准，C 阶段标注「待本地验证」。

**排查预案（承接 u3-l4 的五环节）：** 若 C3 报「符号找不到」，按序检查：run 包是否安装 → `ASCEND_OPP_PATH` 是否指向 vendors → `load_priority` 是否登记 → `nm -D libcust_opapi.so | grep aclnnMyAdd` 确认导出 → csrc 的 `EXEC_NPU_CMD_V1(aclnnMyAdd, ...)` 拼写。若 UT 报 key 断言失败，对照 A3 与 A4 的常量；若 ST 数值错，先查 tiling 的分核边界（尾核余数）与 UB 划拨是否越界。

## 6. 本讲小结

- 新增一个推理算子的最小集合是「九件套」：def、tiling.h/.cpp、kernel.cpp/.h、aclnn 接口 + L0 封装、CMakeLists（infershape 可选），外加 torch 侧「一处 m.def + 一个 csrc 文件」。
- 三个名字必须严格对齐：目录/文件名小写下划线（`my_add`）、OpDef 类名大驼峰（`MyAdd`）、torch schema 与 aclnn 符号（`npu_my_add`/`aclnnMyAdd`）；def 文件名还驱动 autogen 的 `aclnn_my_add.*`/`my_add_proto.*` 生成。
- 构建挂接是三层自动化：func.cmake 的 GLOB 按 `族/算子` 两层结构发现目录并用 `ASCEND_OP_NAME`（`build.sh -n`）过滤；算子 CMakeLists 把源文件分别挂到 `op_host_aclnnInner`（def→cust_opsproto_rt2.0.so）、`optiling`（tiling→cust_opmaster_rt2.0.so）、`opapi`（aclnn→cust_opapi.so）；kernel 以源码形态安装到 vendors 供 opc 编译。
- torch 侧零改打包：csrc 文件放进 `omni_custom_ops/<族>/<算子>/csrc/` 即被 setup.py 的 glob 收集，注册靠 `TORCH_LIBRARY_IMPL` 的 PrivateUse1（真算）与 Meta（推形状）两个调度键。
- 测试闭环 = UT（faker 上下文在纯 CPU 验 tiling 与异常拦截，`bash build.sh -u --ophost -n 'my_add'`）+ ST（真机与 CPU 标杆对拍，`import omni_custom_ops` 不可省、marker 不可漏）。
- TilingKey 与 kernel 入口参数布局是两处「编译器救不了」的人工契约：key 双侧硬编码镜像靠 UT 断言兜底；入口参数顺序必须与 OpDef 的 IO 声明一致、末尾追加 workspace 与 tiling。

## 7. 下一步学习建议

本讲是学习手册推理篇的最后一讲。完成后建议从三个方向继续：

1. **换一个真实算子重走清单**：挑 `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly`（有双核协同与运行期分派）或 `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache`（有多 tiling 模板链），按第 5 节的清单表格逐层对照，体会「最小骨架之上如何长出分支」。
2. **深入多 tiling 模板与并行**：重读 u5-l1（模板轮询与 TilingKey 编码）和 u5-l2（AIV/AIC 协同、FlashDecode），然后思考：如果 `my_add` 要同时支持「单核小张量」与「多核大张量」两条路径，九件套里哪些文件要加第二份实现、TilingKey 怎么分配。
3. **工程化收尾**：阅读 `ascendc/CMakeLists.txt` 的 CPack 段（L768-786）与 `ascendc/build.sh` 的 `--tiling_key`、`--disable-check-compatible` 等参数，结合 u6-l4 的 SOC 适配与发布主题，把你新增的 `my_add` 从「本机可用」推进到「多芯片可分发」。
