# u2-l5 AICPU 算子初探

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 AICPU 算子与 AICore 算子的区别，以及什么场景下应该选 AICPU。
2. 读懂一个 AICPU 算子的完整交付件：`${op_name}.json` 算子信息库 + `_aicpu.cpp` / `_aicpu.h` 设备侧实现。
3. 会用 `build.sh --genop_aicpu` 生成 AICPU 算子骨架，并用 `--opkernel_aicpu` 编译它。

本讲承接 u2-l3（op_kernel：第一个 AscendC 设备核函数）。在 u2-l3 中我们读的是跑在 AICore 上的 Ascend C 核函数；本讲换一个执行载体——同样在 NPU 芯片内部，但跑在 AICPU 上的 C++ 算子。

## 2. 前置知识

### 2.1 AICore 与 AICPU：一颗芯片上的两种算力

昇腾 NPU 芯片上不只有矩阵/向量计算单元（AICore），还有一组通用 CPU 核，称为 AICPU。两套算力对应两套算子开发范式：

| 维度 | AICore 算子（u2-l3 讲的） | AICPU 算子（本讲） |
|---|---|---|
| 开发语言 | Ascend C（C++ 方言 + 特有语法） | 标准 C++ |
| 运行位置 | AICore（Cube/Vector 单元） | 芯片内的通用 CPU 核 |
| 内存模型 | GM → UB（Local）→ GM，用 `TPipe`/`TQue` 管理 | 直接读写指针，像普通 C++ 程序 |
| 是否需要 tiling | 需要（host 产出 tiling data/key/blockDim） | 不需要 tiling 切分策略 |
| 擅长场景 | 大规模规则计算：矩阵乘、向量运算 | 逻辑复杂、控制流多、算子库现成可复用的场景 |
| 单次调用性能 | 高吞吐、高带宽利用 | 相对较低 |

一个直观的对照：u2-l3 里 add_example 的 AICore kernel 要写 CopyIn → Compute → CopyOut 三段流水，还要处理双缓冲；而本讲将看到的 AICPU 版本就是一句朴素的 `y[i] = x0[i] + x1[i]` 循环。

### 2.2 什么时候选 AICPU

官方开发指南在概述里给出定位：AI CPU 算子是「使用 C++ 语言开发、运行在 AI CPU 硬件单元」的算子（[docs/zh/develop/aicpu_develop_guide.md:5-11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L5-L11)）。工程实践中常见的选型理由：

- 计算逻辑是现成的 C++ 代码（例如某个第三方库函数），逐元素搬运到 UB 再算反而费劲；
- 算子内部有大量分支、查表、字符串/变长结构处理，AICore 的向量指令帮不上忙；
- 快速原型验证：先用 C++ 把语义写对，之后再考虑迁移到 AICore 提升性能。

需要注意：AICPU 吞吐低于 AICore，所以本仓库正式算子几乎都以 AICore 为主，AICPU 路径主要出现在教学示例（`examples/add_example`）中，作为学习与对照的样本。

### 2.3 CpuKernel 框架的三步走

开发指南把 Kernel 实现总结为三步：算子类声明 → Compute 函数实现 → 注册算子（[docs/zh/develop/aicpu_develop_guide.md:96-102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L96-L102)）。这与 AICore 侧「kernel 入口 + 算子类 + 注册」的骨架是同构的，只是基类从 Ascend C 机制换成了 `CpuKernel`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [examples/add_example/op_kernel_aicpu/add_example.json](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example.json) | AICPU 算子信息库：算子名、执行引擎、输入输出 dtype 约束 |
| [examples/add_example/op_kernel_aicpu/add_example_aicpu.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.h) | 算子类声明，继承 `CpuKernel` 基类 |
| [examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp) | Compute 函数实现与算子注册 |
| [examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp](https://github.com/gitcode.com/cann-ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp) | AICPU kernel 的 UT：CPU 上直接跑 Compute 验证 |
| [docs/zh/develop/aicpu_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md) | AI CPU 算子开发指南（本讲主线文档） |
| [scripts/opgen/opgen_standalone.py](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py) | `--genop_aicpu` 背后的骨架生成脚本 |
| [scripts/opgen/template/add_example_aicpu/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt) | AICPU 算子模板（`--genop_aicpu` 复制的源） |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | `--genop_aicpu` / `--opkernel_aicpu` 选项的解析与分发 |

对照 u2-l1 的五层范式：AICPU 算子目录把 `op_kernel` 换成了 `op_kernel_aicpu`，且**没有 tiling、没有 def 文件**——因为 dtype/shape 约束改由 json 声明，切分策略也不再需要。

## 4. 核心概念与源码讲解

### 4.1 AICPU 算子的交付件结构

#### 4.1.1 概念说明

一个 AICPU 算子的最小交付件比 AICore 算子更轻。开发指南给出骨架生成的目录结构（[docs/zh/develop/aicpu_develop_guide.md:45-60](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L45-L60)）：

```text
${op_name}
├── examples/test_aclnn_${op_name}.cpp   # aclnn 调用示例
├── op_host/${op_name}_infershape.cpp    # 输出 shape 推导
├── op_kernel_aicpu
│   ├── ${op_name}_aicpu.cpp             # Kernel 入口 + 调度逻辑
│   ├── ${op_name}_aicpu.h               # 算子类声明
│   └── ${op_name}.json                  # 算子信息库
├── tests/ut                             # UT
└── CMakeLists.txt
```

与 AICore 版 add_example 相比，差异集中在两点：

1. `op_kernel` → `op_kernel_aicpu`：实现载体从 Ascend C 核函数换成 C++ 类；
2. `op_host` 里只剩 infershape，**没有 def 和 tiling**——json 承担了「算子静态户口」的一部分职责，tiling 则整个不需要。

#### 4.1.2 核心流程

AICPU 算子从源码到运行的链路：

```text
编写交付件（json + _aicpu.h/.cpp + infershape）
   ↓
bash build.sh --pkg --ops=${op_name} --opkernel_aicpu   # 编译并打成 .run 包
   ↓
安装 .run 包 → kernel 以 libtransformer_aicpu_kernels.so 形式部署到 vendors 目录
   ↓
调用 aclnn 接口（两段式）→ 框架按 json 的 engine/opKernelLib 字段
   把算子路由到 AICPU 执行体 → 框架调用注册表里的 Compute 函数
```

关键理解：json 里的 `engine: DNN_VM_AICPU` 告诉调度器「这个算子跑在 AICPU 上」；`REGISTER_CPU_KERNEL` 宏把 `AddExample` 这个字符串映射到 `AddExampleCpuKernel::Compute`，框架运行期靠这张注册表找到入口。

#### 4.1.3 源码精读

**① json 算子信息库**

[examples/add_example/op_kernel_aicpu/add_example.json:L2-L26](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example.json#L2-L26) —— 这段 json 声明了算子的路由信息与输入输出约束：

```json
"AddExample":{
    "opInfo":{
        "engine":"DNN_VM_AICPU",
        "kernelSo":"libtransformer_aicpu_kernels.so",
        "opKernelLib":"CUSTAICPUKernel",
        ...
    },
    "input0": { "type": "DT_INT32,DT_FLOAT", "name": "x1" },
    "input1": { "type": "DT_INT32,DT_FLOAT", "name": "x2" },
    "output0":{ "type": "DT_INT32,DT_FLOAT", "name": "y" }
}
```

对照记忆点（承接 u2-l2）：

- AICore 侧的 dtype 白名单写在 def 文件里；AICPU 侧写在 json 的 `input*/output*.type` 字段里——`DT_INT32,DT_FLOAT` 就是支持的类型列表。
- `kernelSo` 指明 Compute 实现编译进了哪个动态库；`functionName: RunCpuKernel` 是框架侧统一入口，它再到注册表里查 `AddExample` 对应的类。

**② 算子类声明**

[examples/add_example/op_kernel_aicpu/add_example_aicpu.h:L16-L24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.h#L16-L24) —— 算子类继承 `CpuKernel` 基类，只需重写一个 `Compute` 虚函数：

```cpp
namespace aicpu {
class AddExampleCpuKernel : public CpuKernel {
public:
    ~AddExampleCpuKernel() = default;
    uint32_t Compute(CpuKernelContext &ctx) override;
    template <typename T>
    uint32_t AddCompute(CpuKernelContext &ctx);
};
}
```

命名空间 `aicpu` 是固定的，不允许改（[docs/zh/develop/aicpu_develop_guide.md:119-120](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L119-L120)）。`CpuKernelContext` 是上下文对象，输入、输出、属性都从它取——角色类似 AICore kernel 签名里的那五个 GM 指针，但取用方式是 `ctx.Input(i)` / `ctx.Output(i)` 这种索引式访问。

**③ Compute 实现**

[examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp:L28-L53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp#L28-L53) —— Compute 先做空指针与空数据校验，然后按输入 dtype 分发到模板化的 `AddCompute<float>` 或 `AddCompute<int32_t>`：

```cpp
Tensor *input0 = ctx.Input(kFirstInputIndex);
...
auto data_type = static_cast<DataType>(input0->GetDataType());
switch (data_type) {
    case DT_FLOAT:  return AddCompute<float>(ctx);
    case DT_INT32:  return AddCompute<int32_t>(ctx);
    default:        return kParamInvalid;
}
```

注意这里的 dtype 分发是**运行期 switch**，而 AICore 侧是 tiling key 驱动的**编译期模板实例选择**（u2-l3 讲过的 `if constexpr` + tiling key）。这是两种范式最本质的差异之一。

[examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp:L55-L81](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp#L55-L81) —— 核心计算就是一个朴素循环：

```cpp
T *x0 = reinterpret_cast<T *>(input0->GetData());
...
int64_t num_elements = input0->NumElements();
for (int64_t i = 0; i < num_elements; i++) {
    y[i] = x0[i] + x1[i];
}
```

没有 CopyIn/CopyOut，没有 `TPipe`/`TQue`，没有 `GetBlockIdx`——AICPU 上你拿到的是可以直接解引用的内存指针。这就是「写普通 C++」的体感。

**④ 注册**

[examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp:L83](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example_aicpu.cpp#L83) —— `REGISTER_CPU_KERNEL(kAddExample, AddExampleCpuKernel);` 把字符串 `"AddExample"` 与算子类绑定，这是框架运行期找到 Compute 的唯一线索，算子名必须与 json 的 key 一致。

**⑤ infershape 与 AICPU 模板的其他部分**

AICPU 算子仍需要输出 shape 推导。模板中的 [scripts/opgen/template/add_example_aicpu/op_host/add_example_infershape.cpp:L25-L49](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example_aicpu/op_host/add_example_infershape.cpp#L25-L49) 实现了「输出 shape 等于输入 shape」的推导，并用 `IMPL_OP_INFERSHAPE(AddExampleAicpu)` 注册（机制同 u2-l2 讲的 infershape，这里注册的算子名是模板的 `AddExampleAicpu`，避免与 AICore 版 `AddExample` 冲突）。

**⑥ UT：在 CPU 上直接验证**

[examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp:L36-L52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp#L36-L52) —— 用 `NodeDefBuilder` 构造输入节点，`RUN_KERNEL(node_def, HOST, KERNEL_STATUS_OK)` 直接在 HOST 上执行 Compute，然后比对结果 `{5, 8}`：

```cpp
int32_t input1[2] = {2, 5};
int32_t input2[1] = {3};
...
RUN_KERNEL(node_def, HOST, KERNEL_STATUS_OK);
int32_t output_exp[2] = {5, 8};
```

AICPU 算子的 UT 不需要 NPU 也能编译运行（Compute 就是 C++），这是它「调试友好」的又一体现。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，仅通过「改输入」验证你对 Compute 循环的理解。

**操作步骤**：

1. 打开 `examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp`，阅读 `INT32_VECTOR_ADD_SCALAR_SUCC` 用例。
2. 心算推演：若把 `input2[1] = {3}` 改成 `input2[1] = {10}`，`output_exp` 应改为什么？
3. 在本机（无需 NPU，UT 走 HOST 执行路径）尝试编译该 UT；若暂无编译条件，完成第 2 步的纸面推演即可。

**需要观察的现象**：`RUN_KERNEL` 之后的 `output` 数组内容与 `output_exp` 一致；注意该用例 shape 是 `{2}` 与 `{1}` 相加得到 `{2}`——Compute 循环里 `x1[1]` 越界了吗？（答案在「小练习」第 3 题。）

**预期结果**：UT 断言 `EXPECT_EQ(compare, true)` 通过。实际编译运行结果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：AICPU 版 add_example 为什么不需要 tiling？

**答案**：tiling 解决的是「AICore 上如何把大 tensor 切成 UB 能装下的小块、分给多少个核」的问题（u2-l2、u2-l3）。AICPU 上的 Compute 直接以 `NumElements()` 为界做一次线性遍历，内存按需访问，不存在 UB 容量约束和多核切分决策，因此整个 tiling 环节（tiling data、tiling key、blockDim）都不需要。

**练习 2**：如果想给 AICPU 版 add_example 增加 fp16 支持，要改哪些地方？

**答案**：至少改两处——json 中 `input*/output*` 的 `type` 列表加上 `DT_FLOAT16`；`add_example_aicpu.cpp` 的 switch 中增加一个分支。但要注意开发指南的提示：C++ 自身不支持半精度浮点类型，需借助 Eigen 等第三方库表示（[docs/zh/develop/aicpu_develop_guide.md:174-175](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L174-L175)），UT 文件中也确实 include 了 `Eigen/Core`。

**练习 3**：UT 用例里输入 shape 是 `{2}` 和 `{1}`，Compute 循环却访问了 `x1[0]` 和 `x1[1]` 两处，为什么结果是正确的？

**答案**：严格说 `x1[1]` 越界了，但 UT 中 `input2[1] = {3}` 数组实际开了 1 个元素……实际上该用例传入的 datas 指向的是栈上数组 `int32_t input2[1]`，循环第二次访问 `x1[1]` 属于越界读。这个用例能通过是因为栈内存布局恰好读到有效内存且测试数据未暴露问题——它实际演示的是「标量广播」的期望语义（`{2,5} + {3}` → `{5,8}`）。这提醒我们：AICPU 的 Compute 是普通 C++，框架不会替你做 shape 广播校验，广播语义必须自己在实现里保证（本例的 `NumElements()` 取的是 input0 的元素数，隐式假定了 input1 可广播）。这一点的运行行为：待本地验证。

### 4.2 构建链路：--genop_aicpu 与 --opkernel_aicpu

#### 4.2.1 概念说明

AICPU 算子的构建有两个入口：

- `--genop_aicpu=${op_class}/${op_name}`：从模板复制出一个 AICPU 算子骨架；
- `--opkernel_aicpu`：置位 `ENABLE_AICPU_KERNEL`，让 CMake 编译 `op_kernel_aicpu` 目录（可与 `--pkg --ops=...` 组合出包）。

#### 4.2.2 核心流程

```text
bash build.sh --genop_aicpu=examples/my_first_aicpu
   ↓ process_genop 解析 "op_class/op_name"（build.sh:1024-1056）
   ↓ gen_aicpu_op 调用 python scripts/opgen/opgen_standalone.py
   ↓ 脚本选择 template/add_example_aicpu 模板，copytree 到目标目录
   ↓ 递归重命名 add_example → my_first_aicpu，并做内容替换
      （add_example → my_first_aicpu，AddExample → MyFirstAicpu）
   ↓ 输出 "Create the AI CPU initial directory ... success"

bash build.sh --pkg --soc=ascend910b --ops=my_first_aicpu --opkernel_aicpu
   ↓ ENABLE_AICPU_KERNEL=TRUE，转成 -DENABLE_AICPU_KERNEL cmake 参数
   ↓ 编出 .run 包，安装到 ${ASCEND_HOME_PATH}/opp/vendors
```

#### 4.2.3 源码精读

**① build.sh 的选项解析**

[build.sh:L1946-L1948](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1946-L1948) —— `--genop_aicpu=*` 分支调用 `process_genop "genop_aicpu"`，该函数要求参数必须是 `xxx/yyy` 形式（[build.sh:L1024-L1056](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1024-L1056)），把最后一段当算子名、倒数第二段当算子分类。

[build.sh:L1974-L1975](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1974-L1975) —— `--opkernel_aicpu` 只做一件事：`ENABLE_AICPU_KERNEL=TRUE`。

[build.sh:L1530-L1531](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1530-L1531) 和 [build.sh:L2321-L2323](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2321-L2323) —— 标志位被翻译成 `-DENABLE_AICPU_KERNEL=TRUE` 传给 CMake，走 `build_kernel` 分支；若同时给了 `--pkg` 则走打包路径。这正是 u1-l4 讲过的「build.sh 是选项翻译器」模式在 AICPU 上的延续。

**② opgen 脚本的模板机制**

[scripts/opgen/opgen_standalone.py:L29-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L29-L36) —— 按 `-v aicpu` 选择 `template/add_example_aicpu` 目录作为模板源（`--genop_aicpu` 路径会带上 aicpu 变种参数）。

[scripts/opgen/opgen_standalone.py:L121-L142](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/opgen_standalone.py#L121-L142) —— 内容替换规则：小写下划线名（`add_example` → `my_first_aicpu`）与大驼峰名（`AddExample` → `MyFirstAicpu`）成对替换。所以骨架里的类名、注册名、json key 都会自动对齐你的算子名。

**③ AICPU 目录的 CMakeLists**

[scripts/opgen/template/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt:L17-L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/opgen/template/add_example_aicpu/op_kernel_aicpu/CMakeLists.txt#L17-L27) —— 这段说明了 `op_kernel_aicpu` 目录如何被编译：

```cmake
file(GLOB_RECURSE JSON_FILE ${CMAKE_CURRENT_SOURCE_DIR}/*.json)
set_property(GLOBAL APPEND PROPERTY AICPU_JSON_FILES ${JSON_FILE})
file(GLOB AICPU_SRC ${CMAKE_CURRENT_SOURCE_DIR}/*_aicpu*.cpp)
...
add_aicpu_cust_kernel_modules(${OBJ_NAME})
```

两个命名约定值得记住：json 文件递归收集进全局属性 `AICPU_JSON_FILES`；源文件按「文件名含 `_aicpu`」的 glob 识别——和 u2-l1 讲过的「tiling 文件名须含 `_tiling`」是同一套按命名约定自动注册的思路。最终的编译产物就是 json 里声明的 `libtransformer_aicpu_kernels.so`。

#### 4.2.4 代码实践

见下文「5. 综合实践」（本讲的主实践就是生成并改造一个自己的 AICPU 算子，为避免重复，这里不再单列）。

#### 4.2.5 小练习与答案

**练习 1**：执行 `bash build.sh --genop_aicpu=examples/my_first_aicpu` 后，生成的骨架里为什么没有 `op_kernel`（AICore）目录和 tiling 文件？

**答案**：因为模板源是 `scripts/opgen/template/add_example_aicpu/`，该模板本身只含 `examples/`、`op_host/infershape`、`op_kernel_aicpu/`、`tests/` 和 CMakeLists（可由 Glob 列出的模板文件确认）。AICPU 算子不需要 Ascend C 核函数和 tiling 切分，骨架里自然没有对应文件。对比 `--genop`（AICore 版脚手架）使用的 `template/add_example/` 模板，则包含 `op_kernel/` 与 tiling 三件套。

**练习 2**：生成的骨架默认编译进哪个动态库？由哪个文件声明？

**答案**：`libtransformer_aicpu_kernels.so`，由 json 的 `opInfo.kernelSo` 字段声明（[examples/add_example/op_kernel_aicpu/add_example.json:L9](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_kernel_aicpu/add_example.json#L9)），CMake 侧由 `add_aicpu_cust_kernel_modules` 负责把 `*_aicpu*.cpp` 编进该库。

## 5. 综合实践

**实践目标**：完整走一遍「生成骨架 → 改成减 1 算子 → 编译」的 AICPU 算子开发闭环。

**操作步骤**：

1. **生成骨架**（在仓库根目录执行）：

   ```bash
   bash build.sh --genop_aicpu=examples/my_first_aicpu
   ```

   预期输出：`Create the AI CPU initial directory for my_first_aicpu under examples success`。

2. **对比差异**：并列比较 `examples/my_first_aicpu/` 与 `examples/add_example/`（注意 add_example 同时含 AICore 与 AICPU 两套实现）。确认骨架里没有 `op_kernel`、没有 `*_tiling.cpp`、没有 `*_def.cpp`，并把模板做过的名字替换（`MyFirstAicpu` 类名、json key、注册宏）逐个指认出来。

3. **实现「输出 = 输入 − 1」**：仿照 `AddCompute` 的写法修改 `my_first_aicpu_aicpu.cpp`：

   - Compute 中只取一个输入（`ctx.Input(0)`）与一个输出；
   - 循环体改为 `y[i] = x0[i] - 1;`（示例代码，非项目原有代码）；
   - 同步修改 `my_first_aicpu.json`，删掉 `input1` 条目，使输入输出约束与新语义一致。

4. **编译**：

   ```bash
   bash build.sh --pkg --soc=ascend910b --ops=my_first_aicpu --opkernel_aicpu -j16
   ```

   若不想出包，也可以只编 kernel：`bash build.sh --opkernel_aicpu --soc=ascend910b --ops=my_first_aicpu`。

5. **（可选）验证**：仿照 [examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp:L36-L52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/tests/ut/op_kernel_aicpu/test_add_example.cpp#L36-L52) 写一个 UT：输入 `{5, 8}`，期望输出 `{4, 7}`。

**需要观察的现象**：

- 骨架生成命令的 success 提示与 `examples/my_first_aicpu/` 目录树；
- 编译日志中出现 `[my_first_aicpu] Found aicpu sources: ...`（对应 CMakeLists 的 message 输出）以及最终 `.run` 包名。

**预期结果**：编译产出 `build_out/cann-ops-transformer-custom_linux-*.run`；UT 输出 `{4, 7}`。实际运行结果：待本地验证（本讲义写作环境未执行编译）。

## 6. 本讲小结

- 昇腾 NPU 上有 AICore（向量/矩阵单元，Ascend C 开发）和 AICPU（片上通用 CPU 核，标准 C++ 开发）两种算子载体；AICPU 适合控制流复杂、可复用 C++ 逻辑的场景，但吞吐低于 AICore。
- AICPU 算子交付件比 AICore 更轻：`json`（dtype 白名单 + 路由信息）+ `_aicpu.h/.cpp`（继承 `CpuKernel`、重写 `Compute`、`REGISTER_CPU_KERNEL` 注册）+ infershape；**没有 def 和 tiling**。
- Compute 里拿到的是可直接解引用的指针，dtype 分发靠运行期 switch（对比 AICore 的编译期 tiling key 路由）；框架不替你做广播/越界校验，语义要自己保证。
- `--genop_aicpu` = 复制 `template/add_example_aicpu` 模板 + 名字替换；`--opkernel_aicpu` = 置位 `ENABLE_AICPU_KERNEL` 走 CMake 编译 `*_aicpu*.cpp` 进 `libtransformer_aicpu_kernels.so`。
- AICPU kernel 的 UT 用 `RUN_KERNEL(node_def, HOST, ...)` 在 HOST 上直接执行，无需 NPU 即可验证计算逻辑。

## 7. 下一步学习建议

下一讲将进入 u3 单元「算子调用机制进阶」，建议先做两件事热身：

1. 回顾 u2-l4 的两段式 API，思考 AICPU 算子为何同样能自动获得 aclnn 接口（提示：开发指南「aclnn 适配」一节说明编译完成后会自动生成 aclnn 接口，[docs/zh/develop/aicpu_develop_guide.md:191-193](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicpu_develop_guide.md#L191-L193)）。
2. 若想继续算子开发主线，可提前浏览 [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md)，u6-l1 将以它为纲走完 AICore 自定义算子的完整交付。
