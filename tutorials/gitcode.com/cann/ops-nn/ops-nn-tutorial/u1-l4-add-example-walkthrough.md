# 第一次算子开发实践：修改 AddExample 内核并验证

## 1. 本讲目标

本讲是入门单元的收官实践。读完本讲并完成实践后，你应该能够：

1. 独立完成「修改 Kernel 源码 → 重新编译 → 重新安装 → 重新运行验证」的完整开发闭环。
2. 读懂 `test_aclnn_add_example.cpp` 样例中 aclnn 两段式调用的大致结构，知道每一段在做什么。
3. 掌握修改样例输入 shape 与输入数据的方法，理解为什么改 shape 必须同步改 host 侧数据长度。

本讲不要求你已经理解 tiling 细节和 Ascend C 编程模型（那是第 4、5 单元的内容），只要求你能「照着改、跑得通、看得懂结果」。

## 2. 前置知识

在动手之前，用最通俗的语言回顾几个本讲会用到的概念（详细版本见前几讲）：

- **Kernel（核函数）**：真正跑在 AI Core 上的那段代码。Host 侧（CPU）负责准备数据、下发任务；Device 侧（AI Core）执行 kernel 完成计算。
- **AddExample 算子**：官方提供的教学样例算子，实现逐元素相加 \( z = x + y \)，位于 `examples/add_example`。
- **aclnn 两段式接口**：调用算子的标准方式。第一段 `aclnnXxxGetWorkspaceSize` 完成准备并返回执行器；第二段 `aclnnXxx` 真正把任务下发到 stream 上执行。
- **开发闭环的三条命令**（上一讲已详细讲解）：

  ```bash
  bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16   # 编译
  ./build_out/cann-ops-nn-*linux*.run                                # 安装
  bash build.sh --run_example add_example eager cust --vendor_name=custom  # 运行样例
  ```

- **一个重要经验**：只改样例 cpp 不需要重新编译算子包，直接重跑 `--run_example` 即可；只有改了算子源码（op_host/op_kernel 等）才需要重新编译安装。这是因为样例是每次用 g++ 现场编译的，而算子包是预编译安装的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md) | 官方快速入门文档，本讲实践的主线依据 |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) | Kernel 主体：`AddExample<T>` 模板类，本讲要修改的 `Compute` 就在这里 |
| [examples/add_example/op_kernel/add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp) | Kernel 入口函数：接收 GM 地址，按 tiling key 分发到模板实现 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp) | aclnn 调用样例：构造输入、两段式调用、打印结果，本讲要改它的输入 shape |

## 4. 核心概念与源码讲解

### 4.1 Kernel 的三段式流水与 Compute 函数

#### 4.1.1 概念说明

`AddExample<T>` 是一个模板类，`T` 是数据类型（float 或 int32_t）。它的执行遵循 Ascend C 矢量算子的经典三段式流水：

- **CopyIn**：把数据从 Global Memory（GM，DDR 大内存）搬进 Unified Buffer（UB，AI Core 上的高速缓存）。
- **Compute**：在 UB 里做逐元素计算。
- **CopyOut**：把结果从 UB 搬回 GM。

Compute 是「纯计算」的一环——它只操作 UB 上的 `LocalTensor`，完全不接触 GM。这也是为什么把 Add 改成 Mul 只需要改一行：计算与搬运是解耦的。

#### 4.1.2 核心流程

```text
Process()
  ├── loopCount = ⌈blockLength_ / ubLength_⌉      # 本核数据要分几轮搬
  └── for i in 0..loopCount-1:
        currentNum = 最后一轮取余量，否则取 ubLength_
        CopyIn(i, currentNum)     # GM -> UB
        Compute(currentNum)       # UB 上计算 z = x op y
        CopyOut(i, currentNum)    # UB -> GM
```

其中 `blockLength_` 是分给当前这个核的数据长度，`ubLength_` 是 UB 一轮能装下的长度（都由 tiling 计算好、通过 tiling data 传下来，本讲只需当作已知数）。

#### 4.1.3 源码精读

Compute 函数全文只有几行，先从队列里取出（DeQue）输入、分配（AllocTensor）输出，调用 `AscendC::Add`，再入队、释放：

[examples/add_example/op_kernel/add_example.h:101-111](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L101-L111)

这段代码是本讲实践的核心修改点：`AscendC::Add(zLocal, xLocal, yLocal, currentNum)` 逐元素计算 `zLocal[i] = xLocal[i] + yLocal[i]`，共 `currentNum` 个元素。把它换成 `AscendC::Mul(zLocal, xLocal, yLocal, currentNum)` 就把算子变成了乘法。注意：向量计算指令（Add/Mul 等）的参数格式是一致的（输出、输入1、输入2、元素数），所以可以直接替换。

驱动 Compute 的主循环在 Process 中：

[examples/add_example/op_kernel/add_example.h:113-123](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L113-L123)

这里计算 `loopCount` 并处理尾块（最后一轮 `currentNum` 可能小于 `ubLength_`），逐轮执行 CopyIn → Compute → CopyOut。修改 Add 为 Mul 不需要动这里。

类的整体结构（队列、GM 张量成员）可以先扫一眼留个印象：

[examples/add_example/op_kernel/add_example.h:42-54](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L42-L54)

三个 `TQue` 队列分别缓冲输入 x、输入 y 和输出 z；`BUFFER_NUM = 2` 即双缓冲（第 5 单元细讲）。

QUICKSTART 文档中对应的官方修改指引：

[docs/QUICKSTART.md:118-135](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L118-L135)

#### 4.1.4 代码实践：把 Add 改成 Mul

1. **实践目标**：完成算子内核修改，并通过重新编译、安装、运行验证输出从加法变为乘法。
2. **操作步骤**（前置条件：已按 u1-l2 完成环境准备，能跑通原始 AddExample）：
   1. 编辑 `examples/add_example/op_kernel/add_example.h`，找到第 107 行的 `AscendC::Add(zLocal, xLocal, yLocal, currentNum);`，将其改为（或注释原行后新增）：
      ```cpp
      AscendC::Mul(zLocal, xLocal, yLocal, currentNum);
      ```
   2. 回到项目根目录，重新编译并安装：
      ```bash
      bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16
      ./build_out/cann-ops-nn-*linux*.run
      ```
   3. 重新运行样例：
      ```bash
      bash build.sh --run_example add_example eager cust --vendor_name=custom
      ```
3. **需要观察的现象**：输出中 `result` 的值从 `2.000000`（1+1）变为 `1.000000`（1×1），`first input` / `second input` 仍为 `1.000000`。
4. **预期结果**（对照 QUICKSTART 的说明）：

   ```text
   add_example first input[0] is: 1.000000, second input[0] is: 1.000000, result[0] is: 1.000000
   add_example first input[1] is: 1.000000, second input[1] is: 1.000000, result[1] is: 1.000000
   ...
   ```

   完整预期输出见 [docs/QUICKSTART.md:139-173](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L139-L173)。本教程编写环境无昇腾硬件，以上为文档给出的预期，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AscendC::Mul` 写成了 `AscendC::Mul(yLocal, xLocal, zLocal, currentNum)`（参数顺序打乱），会发生什么？

**答案**：计算结果会写进 `yLocal`（输入缓冲）而不是 `zLocal`，随后 `CopyOut` 搬出的 `zLocal` 是未初始化数据，输出错误。Ascend C 的矢量接口约定为「输出在前、输入在后、元素数最后」，参数顺序不能随意交换。

**练习 2**：为什么修改 kernel 后必须重新 `--pkg` 编译并重装 run 包，而不能只重跑 `--run_example`？

**答案**：`--run_example` 每次只是现场用 g++ 编译样例 cpp 并链接已安装的算子库；算子 kernel 的机器码在 run 包里，样例重跑并不会重新编译 kernel。改 kernel 必须重新打包安装，才能让新的二进制生效。

### 4.2 Kernel 入口函数：从 aclnn 调用到模板实例

#### 4.2.1 概念说明

样例调用的是 `aclnnAddExample`，而 kernel 文件里却是一个 `__global__ __aicore__` 函数。中间的桥梁是：aclnn 适配层根据算子原型和 tiling 结果决定调用哪个 kernel 入口、用什么 tiling key。入口函数再根据 tiling key 把任务实例化成具体数据类型（float / int32_t）的 `AddExample<T>`。

#### 4.2.2 核心流程

```text
样例 main()
  └─ aclnnAddExampleGetWorkspaceSize / aclnnAddExample     # Host 侧（第 2、6 单元细讲）
       └─ CANN runtime 按 tiling key 选择 kernel 二进制
            └─ add_example<schMode>(x, y, z, workspace, tiling)   # Device 侧入口
                 ├─ GET_TILING_DATA_WITH_STRUCT 读取 tiling data
                 └─ 实例化 NsAddExample::AddExample<float 或 int32_t>
                      ├─ Init(...)    # 设置 GM 地址、初始化 UB 队列
                      └─ Process()    # 三段式流水
```

#### 4.2.3 源码精读

kernel 入口函数与 tiling key 枚举：

[examples/add_example/op_kernel/add_example.cpp:24-27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L27)

定义了两个 tiling key：0 对应 float 实现，1 对应 int32 实现，用于区分不同数据类型的实现策略。

[examples/add_example/op_kernel/add_example.cpp:36-57](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L36-L57)

这是 kernel 入口：先用 `GET_TILING_DATA_WITH_STRUCT` 从 GM 的 tiling 区域读出 Host 侧写好的 tiling data（这就是 u1-l3 提到的「Host 写、Device 读的数据契约」），再按模板参数 `schMode` 用 `if constexpr` 分发到 `AddExample<float>` 或 `AddExample<int32_t>`。注意本讲改的 `Compute` 在模板类里，float 和 int32 两条分支会**同时**变成 Mul。

Init 中根据 tiling data 计算本核负责的数据段并初始化队列：

[examples/add_example/op_kernel/add_example.h:56-70](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h#L56-L70)

本讲不需要改它，但要知道：`blockFactor * GetBlockIdx()` 决定了「第几个核处理第几段数据」，`pipe.InitBuffer` 为三个队列各分配 `ubLength_ * sizeof(T)` 的 UB 空间。

#### 4.2.4 代码实践：验证两条数据类型分支都被修改

1. **实践目标**：确认模板修改对 float 和 int32 两个分支同时生效，加深对「模板类 + tiling key 分发」的理解。
2. **操作步骤**：
   1. 阅读 [add_example.cpp:45-56](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L45-L56)，确认两个 `if constexpr` 分支实例化的是同一个模板类。
   2. 依次把样例中的 `aclDataType::ACL_FLOAT`（位于 [test_aclnn_add_example.cpp:102](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L102)）改为 `ACL_INT32`、host 数据 vector 改为 `std::vector<int32_t>`，重新运行观察 int32 分支的乘法输出。
3. **需要观察的现象**：int32 输入下输出同样变为乘法结果（例如 2×3=6）。
4. **预期结果**：两条分支行为一致。注意样例第 99 行有注释「当前样例算子未进行 shape、dtype 全泛化，其他输入场景可能存在不支持情况」，若 int32 路径报错属正常现象，记录报错信息即可，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：入口函数中 `REGISTER_TILING_DEFAULT` 和 `GET_TILING_DATA_WITH_STRUCT` 各起什么作用？

**答案**：前者向框架注册本算子使用的默认 tiling 数据结构类型；后者把 GM 上 tiling 区域的内容按该结构体解释并拷贝到局部变量 `tilingData`，使 kernel 内可以直接以字段方式访问 `totalNum`、`blockFactor`、`ubFactor` 等 Host 侧算好的切分参数。

**练习 2**：为什么用 `if constexpr` 而不是普通 `if`？

**答案**：`if constexpr` 在编译期裁剪分支，两个分支各自编译成独立的 kernel 二进制（由 tiling key 选择），模板参数 `schMode` 是编译期常量，普通 `if` 无法用编译期常量条件比较的方式这样裁剪，也起不到按类型生成多份实现的作用。

### 4.3 aclnn 调用样例的结构：两段式接口与结果打印

#### 4.3.1 概念说明

`test_aclnn_add_example.cpp` 是一个标准的 aclnn 调用样板，整体骨架对任何算子都通用：

1. 初始化 ACL 运行时（`aclInit` / `aclrtSetDevice` / `aclrtCreateStream`）。
2. 构造输入输出 aclTensor（host 数据 → device 内存 → `aclCreateTensor`）。
3. 第一段接口 `aclnnAddExampleGetWorkspaceSize`：拿到 `workspaceSize` 和执行器 `executor`。
4. 按 workspaceSize 申请 device 内存，第二段接口 `aclnnAddExample` 下发执行。
5. `aclrtSynchronizeStream` 等待完成，把结果拷回 host 打印。

#### 4.3.2 核心流程

```text
main()
 └─ aclnnAddExampleTest(deviceId, stream)
     ├─ Init: aclInit → aclrtSetDevice → aclrtCreateStream
     ├─ CreateAclTensor × 3: malloc device 内存 → H2D 拷贝 → aclCreateTensor
     ├─ aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, &workspaceSize, &executor)
     ├─ aclrtMalloc(workspace) （仅当 workspaceSize > 0）
     ├─ aclnnAddExample(workspaceAddr, workspaceSize, executor, stream)
     ├─ aclrtSynchronizeStream(stream)
     └─ PrintOutResult: D2H 拷贝并打印前 10 个元素
```

#### 4.3.3 源码精读

构造 aclTensor 的通用模板函数（host 数据先拷到 device，再按连续内存的 strides 创建描述符）：

[examples/add_example/examples/test_aclnn_add_example.cpp:66-88](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L66-L88)

两段式调用的关键代码：

[examples/add_example/examples/test_aclnn_add_example.cpp:130-145](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L130-L145)

第 131 行是第一段（准备阶段，返回执行器），第 144 行是第二段（把执行器挂到 stream 上真正运行）。中间按需申请 workspace。这就是 u1-l3 提到的「aclnn 两段式接口」的具体形态，第 2 单元会系统展开。

默认输入定义（本讲 4.4 要修改的对象）：

[examples/add_example/examples/test_aclnn_add_example.cpp:99-102](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L99-L102)

默认 shape 为 `{32, 4, 4, 4}`（共 2048 个元素），数值全为 1。selfY 与 out 的构造在下方以相同方式重复（第 108-124 行）。

结果打印（只打印前 10 个元素，从 device 拷回 host）：

[examples/add_example/examples/test_aclnn_add_example.cpp:38-52](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L38-L52)

#### 4.3.4 代码实践：通读样例并标注调用链

1. **实践目标**：不借助工具，能在纸上写出从 `main` 到结果打印的调用链。
2. **操作步骤**：
   1. 打开样例文件，从 `main`（第 156 行）开始向下逐函数阅读。
   2. 对每个函数标注它属于骨架五步中的哪一步（初始化 / 构造张量 / 两段调用 / 同步 / 打印）。
   3. 用 `Grep` 在 `examples/add_example/op_api` 下搜索 `aclnnAddExampleGetWorkspaceSize`，找到它的声明位置，确认样例 include 的 `aclnn_add_example.h` 来自哪个目录。
3. **需要观察的现象**：能明确指出 `executor` 在第一段产生、第二段消费；workspace 只在 `workspaceSize > 0` 时申请。
4. **预期结果**：得到一张五步调用链笔记，为第 2 单元（aclnn 调用方式）做好准备。纯阅读型实践，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：`CreateAclTensor` 中 strides 是怎么算出来的？shape `{8,8,8,8}` 对应的 strides 是什么？

**答案**：从倒数第二维往前累乘：`strides[i] = shape[i+1] * strides[i+1]`，描述连续内存布局下每一维跳一格的元素数。`{8,8,8,8}` 的 strides 是 `{512, 64, 8, 1}`。

**练习 2**：为什么样例用 `std::unique_ptr` 包住 aclTensor 和 device 内存指针？

**答案**：利用 RAII 在作用域结束时自动调用 `aclDestroyTensor` / `aclrtFree`，即使中途 return 也不泄漏 device 资源。这是样例提供的固定写法，实际编写时推荐沿用。

### 4.4 修改输入 shape 与数据：验证算子的泛化行为

#### 4.4.1 概念说明

算子不是只为一个 shape 服务的。样例默认 `{32,4,4,4}`、全 1 数据，只能验证「跑通了」，不能验证「算对了」。修改输入 shape 与填充有区分度的数据（如 0-9 循环值），是最小成本的泛化验证手段。

关键约束：**shape、host 数据 vector 长度、输出 vector 长度三者必须一致**。`CreateAclTensor` 按 `GetShapeSize(shape) * sizeof(T)` 申请并拷贝 device 内存，vector 长度与 shape 乘积不一致会导致数据越界或结果不匹配。

#### 4.4.2 核心流程

```text
修改三处（selfX / selfY / out）：
  shape:  {32,4,4,4}                → {8,8,8,8}
  vector: 2048 个元素                → 4096 个元素（8×8×8×8）
  数据:   全 1                      → 可选：i % 10 循环值（更有区分度）
然后：无需重新编译算子包，直接重跑 --run_example
```

#### 4.4.3 源码精读

QUICKSTART 给出的官方修改示例：

[docs/QUICKSTART.md:241-260](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L241-L260)

注意文档第 260 行的强调：`selfX`、`selfY`、`out` 三者的 vector 长度都要从 2048 改为 4096，并保证三者 shape 一致。

对应到样例源码，需要修改的三个位置分别是：

- selfX：[test_aclnn_add_example.cpp:100-101](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L100-L101)
- selfY：[test_aclnn_add_example.cpp:110-111](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L110-L111)
- out：[test_aclnn_add_example.cpp:119-120](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L119-L120)

修改后无需重新编译算子包、直接重跑验证的说明：

[docs/QUICKSTART.md:262-272](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L262-L272)

#### 4.4.4 代码实践：shape 改为 {8,8,8,8} 并验证

1. **实践目标**：验证算子在新的输入规模与非常数数据下仍计算正确。
2. **操作步骤**：
   1. 在 4.1 实践已完成（kernel 为 Mul）的基础上，编辑 `examples/add_example/examples/test_aclnn_add_example.cpp`：
      ```cpp
      // selfX 处
      std::vector<int64_t> selfXShape = {8, 8, 8, 8};
      std::vector<float> selfXHostData(4096);
      for (int i = 0; i < 4096; ++i) {
          selfXHostData[i] = static_cast<float>(i % 10);   // 0-9 循环值
      }
      ```
   2. 对 selfY 做同样修改（可用 `i % 7` 等不同周期，便于区分两个输入）；out 处把 shape 改为 `{8, 8, 8, 8}`、vector 长度改为 4096（输出数据本身会被覆盖，初始值随意）。
   3. 只重跑样例（不重新编译算子包）：
      ```bash
      bash build.sh --run_example add_example eager cust --vendor_name=custom
      ```
3. **需要观察的现象**：打印出的 `result[i]` 应等于 `first input[i] × second input[i]`。例如若 selfX 取 `i % 10`、selfY 取 `i % 7`，则前 10 个元素应为 0×0=0, 1×1=1, 2×2=4, 3×3=9, 4×4=16, 5×5=25, 6×6=36, 0×7=0（i=7 时 7%10=7、7%7=0）, 8×1=8, 9×2=18。
4. **预期结果**：输出与上述手算值一致，说明 Mul kernel 在 4096 元素、非常数输入下正确。本教程编写环境无昇腾硬件，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 selfX 的 shape 改成 `{8,8,8,8}` 但 `selfXHostData` 仍是 2048 个元素，会发生什么？

**答案**：`CreateAclTensor` 按 shape 乘积 4096 申请 device 内存并把 4096×4 字节从 host 拷到 device，而 host vector 只有 2048×4 字节，`aclrtMemcpy` 读取越界（未定义行为）或报拷贝失败；即使侥幸通过，后 2048 个元素也是垃圾数据。shape 与数据长度必须同步。

**练习 2**：为什么修改样例后不需要重新 `--pkg`？

**答案**：`--run_example` 每次都重新编译样例 cpp，样例只是算子的调用方；算子二进制没有变化，自然不需要重新打包安装。

**练习 3**：打印只显示前 10 个元素，想看第 1000 个元素怎么办？

**答案**：修改 `PrintOutResult` 中 `std::min(GetShapeSize(shape), static_cast<int64_t>(10))` 的 10（见 [test_aclnn_add_example.cpp:42](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L42)），函数内已有注释提示这一点。

## 5. 综合实践

把本讲全部内容串成一条完整链路，模拟一次最小版的「算子迭代」：

1. 在干净环境中跑通原始 AddExample（输出 2.000000）。
2. 修改 `add_example.h` 的 `Compute`：Add → Mul，重新编译、安装、运行，确认输出变为乘法。
3. 修改样例：shape 改为 `{8,8,8,8}`，selfX 填 `i % 10`、selfY 填 `i % 7`，vector 长度改 4096；不重编算子包，直接重跑 `--run_example`，用计算器抽查 5 个下标的手算值与输出对账。
4. 写一份简短的实验记录，包含三栏：改动点（文件:行号）、命令、输出变化。这份记录格式在第 8 单元（调试与性能调优）会继续沿用。

如果某一步输出不符，回到对应小节的「需要观察的现象」逐项排查——最常见的两个错误是：忘记重新安装 run 包（第 2 步）、vector 长度没同步（第 3 步）。

## 6. 本讲小结

- Ascend C 矢量算子 kernel 遵循 CopyIn → Compute → CopyOut 三段式流水，计算与搬运解耦，改一行 `AscendC::Add` 为 `AscendC::Mul` 即可换算子语义。
- kernel 入口函数用 `GET_TILING_DATA_WITH_STRUCT` 读取 Host 写好的 tiling data，再按 tiling key 用 `if constexpr` 分发到 `AddExample<float>` / `AddExample<int32_t>` 模板实例。
- aclnn 样例是通用五步骨架：初始化 → 构造 aclTensor → 两段式调用（GetWorkspaceSize + 执行）→ 同步 → 拷回打印。
- 开发闭环的节奏感：改算子源码必须重新 `--pkg` 编译并安装 run 包；只改样例直接重跑 `--run_example`。
- 修改输入 shape 时，shape 乘积、host 数据 vector 长度、输出 vector 长度三者必须一致，否则越界或结果错误。

## 7. 下一步学习建议

下一讲（u2-l1「aclnn API 调用算子」）将把本讲 4.3 节的两段式调用讲透：以 `activation/gelu/op_api/aclnn_gelu.cpp` 为样本，理解 aclnn 适配层如何把框架调用转成算子执行，并动手写一个调用 `aclnnGelu` 的最小样例。建议提前通读 `examples/add_example/examples/test_aclnn_add_example.cpp` 全文，你会发现 gelu 的样例几乎是同一个骨架换一个算子名。
