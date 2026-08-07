# 测试套件组织与 CI 流水线

## 1. 本讲目标

本讲是测试单元 U9 的第二篇，承接 u9-l1（你已经知道项目用 Chisel 6 的 `EphemeralSimulator` 而非 `chiseltest`，也熟悉了 `testUtil` 共享工具和 ChiselEnum 的 `peek` 技巧）。本讲把视角从「怎么写一个 spec」拉远到「整个测试套件长什么样、怎么只跑其中一个、远端 CI 又是怎么自动跑全部的」。

学完后你应该能够：

- 看懂 `src/test/scala` 下「与源码目录镜像」的测试组织方式，并区分单元测试与集成测试。
- 用 `tool/test-specific-spec.sh` 只跑某个 Spec，并借助 `-oDT` 输出对比不同 spec 的耗时。
- 逐行解释 `.github/workflows/actions.yml`，说出 CI 在 push 到 `main` 时执行的确切命令序列。
- 区分「CI 里直接调 sbt」与「本地用 make 包一层 docker」这两条路径的差异。

## 2. 前置知识

- **EphemeralSimulator**：Chisel 6 自带的「用完即弃」仿真器，每次 `simulate(new Module)` 都会现场 elaborate → firtool 编译 → verilator 仿真（详见 u9-l1）。这带来一个重要后果：**每个 `simulate(...)` 调用都有一笔固定的「编译 + elaborate」开销**，被测模块越大，这笔开销越高。本讲讲耗时对比时，根因就在这里。
- **sbt（Scala Build Tool）**：项目的构建工具。`sbt run` 运行 `top.Main` 把设计 elaborate 成 `top.sv`；`sbt test` 编译并运行所有 `*Spec`；`sbt "testOnly <类名>"` 只跑指定的 Spec。
- **Docker 镜像 `fangruil/chisel-dev:amd64`**：打包好 firtool 1.62.1、verilator v5.036、SystemC 3.0.1 以及 `publishLocal` 过的 Chisel 6.7.0，保证「同一套工具链」在任何机器上可复现（详见 u1-l2）。
- **GitHub Actions**：GitHub 内置的 CI（持续集成）服务，用 YAML 文件描述「什么事件触发、在什么环境里跑哪些命令」。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/test/scala/isa/InstrDecoderSpec.scala` | 译码器单元测试，本讲作为「单测」代表逐段精读 |
| `src/test/scala/backend/NCoreBackendQuantSpec.scala` | 后量化集成测试，本讲作为「集成测试」代表逐段精读 |
| `.github/workflows/actions.yml` | CI 流水线定义：触发条件 + Lint/Build/Test 三个 job |
| `tool/test-specific-spec.sh` | 只跑某一个 Spec 的快捷脚本 |
| `tool/test-all.sh` | 跑全部测试（等价于 `make test`） |
| `build.sbt` | 含 `-oDT` 测试选项，控制 spec 耗时输出 |
| `Makefile` | 本地 `test` 目标，把 sbt 包进 docker |
| `AGENTS.md` | 仓库给 agent 的权威说明，含测试与 CI 段落 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看测试套件的目录组织与分层（4.1），再看只跑单测的快捷方式与耗时输出（4.2），最后解析 CI 流水线（4.3）。

### 4.1 测试套件的镜像目录组织与分层

#### 4.1.1 概念说明

一个 NPU 项目写到 20 个测试文件时，最怕的不是测试本身，而是「找」。chisel-npu 用一个非常朴素但有效的约定解决这个问题：**测试目录树和源码目录树长得一模一样（镜像）**。

源码里 `src/main/scala/alu/pe/procElem.scala` 定义了 PE，那么它的测试就放在 `src/test/scala/alu/pe/PESpec.scala`——把 `main` 换成 `test`，文件名换成 `<模块名>Spec.scala`，路径其余部分保持不变。这样你看到任何一个源文件，闭着眼睛也能推出它的测试在哪里（如果有的话）。

在镜像之上，还有一条「**分层**」的隐含约定：能被某个模块单独验证的，写成**单元测试（unit test）**；只有把多个模块组装起来才能验证的，写成**集成测试（integration test）**。这两层落在不同的目录里。

#### 4.1.2 核心流程

镜像与分层的判定流程：

1. 拿到一个源码文件，比如 `src/main/scala/alu/vec/vec.scala`。
2. 想找它的测试 → 去掉 `main`，改成 `src/test/scala/alu/vec/`，找 `VALU*Spec.scala`。
3. 判断它是单元还是集成：
   - 若被测对象是**单个模块**（PE、DataFeeder、VALU、InstrDecoder、MultiWidthRegisterBlock、MMALU），即单元测试。
   - 若被测对象是**完整后端 `NCoreBackend`**（译码器 + 寄存器堆 + MMALU + VALU 全部连起来），即集成测试，固定落在 `backend/` 目录。
4. 没有可测逻辑的（如 `top/top.scala` 只负责 elaborate）就不写测试。

用一张表概括整个 `src/test/scala` 树（共 20 个 `*Spec.scala`）：

| 镜像源码目录 | 对应测试文件 | 层级 | 被测对象 |
|:---|:---|:---:|:---|
| `alu/pe/` | `PESpec.scala` | 单元 | 单个 PE（乘累加） |
| `alu/mma/` | `MMALUSpec.scala`、`MMALUStreamReduceSpec.scala` | 单元 | MMALU 顶层（含 K×K 阵列） |
| `alu/mma/sa/` | `DataFeederSpec.scala`、`DataCollectorSpec.scala` | 单元 | 阵列的数据馈送/收集 |
| `alu/mma/cu/` | `CUSpec.scala` | 单元 | 控制单元 |
| `alu/vec/` | 8 个 `VALU*Spec.scala`（Arith/Logic/MinMax/Reduce/Cast/FP32/Cvt/Activation/ProgrammableLut） | 单元 | VALU（K=8） |
| `isa/` | `InstrDecoderSpec.scala` | 单元 | 组合译码器 |
| `sram/` | `MultiWidthRegisterSpec.scala`、`RegisterSpec.scala` | 单元 | 多宽度寄存器堆 |
| `backend/` | `NCoreBackendQuantSpec.scala`、`NCoreBackendGemmSoftmaxSpec.scala` | **集成** | 完整 `NCoreBackend` |
| `utils/` | `printHelper.scala`、`widthHelper.scala` | — | 共享测试工具（**不是 Spec**） |
| `top/` | （无） | — | `top.scala` 仅 elaborate，无可测逻辑 |

注意两点：`utils/` 里的文件以 `Helper` 结尾而非 `Spec`，它们是 `testUtil` 包里的工具（见 u9-l1），sbt 不会把它们当测试跑；`top/` 在测试侧是空的，因为 `top.scala` 只做 elaborate、没有可断言的行为。

#### 4.1.3 源码精读

**单元测试代表：`InstrDecoderSpec`。** 它只实例化一个纯组合译码器 `InstrDecoder`，每一拍 `poke` 一个 32 位指令字、`step(0)` 求组合逻辑、再 `expect` 各字段。被测对象单一、规模小，是典型的单元测试。

[src/test/scala/isa/InstrDecoderSpec.scala:11-24](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L11-L24) — 类继承 `AnyFlatSpec`，导入 `NpuAssembler._` 用汇编器构造指令字；`check` 辅助函数封装「poke 指令 → step(0) → 检查 illegal 与各解码字段」的标准套路。

[src/test/scala/isa/InstrDecoderSpec.scala:44-50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50) — 第一个测试用例「decode vadd VX」：在一个 `simulate(new InstrDecoder)` 里验证 `vadd` 的宽度、rd、rs1、rs2 被正确解码。

[src/test/scala/isa/InstrDecoderSpec.scala:238-245](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L238-L245) — 「非法指令」测试：poke 一个保留 opcode `0x7F`，断言 `illegal` 被拉高。整个文件有二十多个这样的 `"X" should "Y" in` 用例，但每次只 elaborate 一个极小的组合模块。

**集成测试代表：`NCoreBackendQuantSpec`。** 它不再测单个模块，而是实例化完整的 `NCoreBackend`（译码器 + 多宽度寄存器堆 + MMALU + VALU 全部连起来），通过外部读写端口灌数据、发指令、读结果，验证一条端到端流水线。

[src/test/scala/backend/NCoreBackendQuantSpec.scala:1-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L1-L9) — 文件头注释直接写明了集成测试要验证的七步后量化流水线（INT8 经 MMA 累加 → FP32 → 缩放加偏置 → 回 INT8），这正是「只有把模块组装起来才能验证」的场景。

[src/test/scala/backend/NCoreBackendQuantSpec.scala:142-153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L142-L153) — 只有一个 `simulate(new NCoreBackend(K, N, 32))` 调用，却把 7 个子用例合并进同一个 DUT 实例（用 `withClue` 给每段标上下文）。被测对象是整颗后端，规模远大于译码器。

一个需要诚实指出的细节：`runFullQuantSequence` 目前只断言整条量化流水线的每条指令**译码合法**（`!illegal_out`），并没有在 DUT 上做整条链的 bit-exact 数值比对；真正做 bit-exact 校验的是文件末尾那段**纯 Scala、无 DUT** 的属性测试：

[src/test/scala/backend/NCoreBackendQuantSpec.scala:157-182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L157-L182) — 用 `FpRef`（封装 `java.lang.Float`）在纯 Scala 里镜像硬件的量化计算，断言与黄金参考在 1 ULP 内一致。它不经过仿真器，因此是「集成测试里的数值正确性兜底」。理解这一点有助于你正确估计集成测试的实际覆盖（承接 u7-l1 的结论）。

#### 4.1.4 代码实践

**实践目标**：用「镜像约定」在源码与测试之间双向导航。

1. 打开本仓库，找到 `src/main/scala/alu/mma/sa/dataFeeder.scala`。
2. 按镜像约定推断它的测试文件路径（把 `main` 换成 `test`）。
3. 打开推断出的路径，确认 `DataFeederSpec.scala` 确实存在。
4. 反向练习：看到 `src/test/scala/sram/MultiWidthRegisterSpec.scala`，推出它测的是 `src/main/scala/` 下哪个文件。
5. **需要观察的现象**：每一条推断都能在文件系统里命中，不需要搜索。
6. **预期结果**：`dataFeeder.scala` → `src/test/scala/alu/mma/sa/DataFeederSpec.scala`；`MultiWidthRegisterSpec.scala` → `src/main/scala/sram/multiWidthRegister.scala`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `top/` 在测试侧是空的？

<details><summary>参考答案</summary>

因为 `top.scala` 里的 `object Main` 只调用 `ChiselStage.emitSystemVerilog` 把设计 elaborate 成 `top.sv`，本身没有任何可断言的运行时行为（没有输入输出端口可以 poke/expect）。对一个「只做 elaborate」的对象写仿真测试没有意义，因此没有对应 Spec。
</details>

**练习 2**：`NCoreBackendQuantSpec` 为什么算集成测试而不是单元测试？

<details><summary>参考答案</summary>

它实例化的是 `new NCoreBackend(K, N, 32)`——完整的后端，内部把译码器、多宽度寄存器堆、MMALU、VALU 全部连起来。它要验证的是「这些模块组装后能否协同完成一条端到端量化流水线」，这种「跨模块协作」正是集成测试的职责；单元测试则只针对单个模块（如 `InstrDecoderSpec` 只测译码器）。
</details>

---

### 4.2 单测快捷方式：test-specific-spec.sh 与耗时输出

#### 4.2.1 概念说明

`make test`（等价于 `sbt test`）会跑**全部** 20 个 Spec。但开发时你通常只想验证自己刚改的那个模块——比如改完译码器只想跑 `InstrDecoderSpec`。每次跑全套既慢又浪费，因为 EphemeralSimulator 对每个 `simulate(...)` 都要现场编译。

项目提供了两个 shell 脚本解决这个问题。`tool/test-specific-spec.sh` 接收一个**全限定类名**（fully-qualified class name，即「包名.类名」），只跑那一个 Spec；`tool/test-all.sh` 则跑全部。

此外，`build.sbt` 里悄悄配了一个测试选项 `-oDT`，它会在测试结束后**按耗时从慢到快打印每个用例的耗时**——这正是后面「对比单测与集成测试耗时」实践的数据来源。

#### 4.2.2 核心流程

只跑单测的执行链路：

1. 你在命令行敲：`tool/test-specific-spec.sh <全限定类名>`。
2. 脚本把参数 `$1` 拼进 `sbt "testOnly $1"`。
3. 整条命令在 `fangruil/chisel-dev:amd64` 容器里执行（容器内已备好 firtool/verilator/Chisel）。
4. sbt 只编译并运行你指定的那一个 Spec 类。
5. 测试结束后，`-oDT` 把该 Spec 内各用例的耗时排序输出。

关键点：传给脚本的必须是**全限定类名**，由 Scala 源文件顶部的 `package` 声明决定。比如 `InstrDecoderSpec.scala` 第 4 行 `package isa` → 全限定名 `isa.InstrDecoderSpec`；`NCoreBackendQuantSpec.scala` `package backend` → `backend.NCoreBackendQuantSpec`。

#### 4.2.3 源码精读

[tool/test-specific-spec.sh:1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-specific-spec.sh#L1) — 整个脚本就一行：`docker run --rm --env SBT_OPTS="-Xmx8G -Xss2M" -v ${PWD}:/workspace/ fangruil/chisel-dev:amd64 sbt "testOnly $1"`。`$1` 是你传入的全限定类名；`--rm` 表示跑完即删容器；`-Xmx8G` 给 JVM 8GB 堆（大规模 elaborate 很吃内存）。

[tool/test-all.sh:1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-all.sh#L1) — 同样一行，但跑的是 `sbt test`（全部），与 `Makefile` 的 `test` 目标等价。

[build.sbt:25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L25) — `Test / testOptions += Tests.Argument(TestFrameworks.ScalaTest, "-oDT")`。`-oDT` 是 ScalaTest 的 reporter 参数：`-o` 输出到标准输出，`D` 显示每个测试的 Duration（耗时），`T` 按耗时排序（慢的在前）。这一行决定了无论你跑单测还是全套，都能看到「谁最慢」。

为什么 `NCoreBackendQuantSpec` 即便用例数少，单次 `simulate` 也很可能比 `InstrDecoderSpec` 的单次 `simulate` 慢得多？根因回到 EphemeralSimulator 的「按需现场编译」：`simulate(new NCoreBackend(8,8,32))` 要 elaborate 一颗含 8×8 脉动阵列、VALU、多宽度寄存器堆的完整后端，DUT 规模远大于 `simulate(new InstrDecoder)` 的单个组合模块，elaborate + firtool + verilator 三步的编译代价因此高得多。`-oDT` 会把这个差距量化出来。

#### 4.2.4 代码实践

**实践目标**：只跑单个 Spec，并用 `-oDT` 输出对比单元测试与集成测试的耗时。

1. 确认本机已装 Docker（脚本依赖它），或已在容器/镜像内（见 u1-l2）。
2. 跑译码器单测（全限定类名 `isa.InstrDecoderSpec`）：
   ```
   tool/test-specific-spec.sh isa.InstrDecoderSpec
   ```
3. 跑后量化集成测试（全限定类名 `backend.NCoreBackendQuantSpec`）：
   ```
   tool/test-specific-spec.sh backend.NCoreBackendQuantSpec
   ```
4. **需要观察的现象**：两次运行的末尾，ScalaTest 都会打印一段按耗时排序的用例列表（`-oDT` 的效果）。
5. **预期结果**：
   - 两条命令都应 PASS。
   - `NCoreBackendQuantSpec` 里那条 `simulate(new NCoreBackend(...))` 的耗时，明显大于 `InstrDecoderSpec` 里单个 `simulate(new InstrDecoder)` 的耗时——因为前者的 DUT（完整后端含 8×8 阵列）规模大得多。
   - 若你的环境无 Docker / 无 firtool，本步骤无法实际运行，标注「待本地验证」，但你仍可从「被测模块规模」推断耗时排序的合理性。

> 提示：若想一次只跑某个 Spec 里的**某个用例**，可绕过脚本直接用 sbt 选择器，例如 `sbt "testOnly isa.InstrDecoderSpec -- -z "decode vadd VX""`（`-z` 按测试名子串过滤）。这是脚本未封装、但 sbt 原生支持的能力。

#### 4.2.5 小练习与答案

**练习 1**：你想只跑 `VALUArithSpec`，应该传给脚本的字符串是什么？依据是什么？

<details><summary>参考答案</summary>

`alu.vec.VALUArithSpec`。依据是该 Spec 源文件顶部的 `package` 声明（`package alu.vec`）加上类名 `VALUArithSpec`。sbt 的 `testOnly` 需要全限定类名来定位。
</details>

**练习 2**：如果不传任何参数直接跑 `tool/test-specific-spec.sh`，会发生什么？

<details><summary>参考答案</summary>

`$1` 为空，sbt 收到的命令变成 `sbt "testOnly "`，sbt 会报「没有指定测试」之类的错误而不会跑任何测试。脚本不做参数校验，需要调用者自己保证传入合法的全限定类名。
</details>

---

### 4.3 GitHub Actions CI 流水线解析

#### 4.3.1 概念说明

CI（持续集成）的目标是：每次有人往仓库 push 代码、或提 PR，远端就自动在一个干净环境里把项目构建并测一遍，让「坏代码」在合并前就被挡住，而不是等下次某人本地 `make test` 时才暴雷。

chisel-npu 的 CI 定义在 `.github/workflows/actions.yml`，结构非常精简：三个按顺序执行的 job——**Lint**（检出冒烟）、**Build**（elaborate 出 SystemVerilog）、**Test**（跑全部测试）。后两个 job 在项目自带的 `fangruil/chisel-dev:amd64` 容器里直接调用 sbt，**不经过 `make`**——因为容器里已经把工具链备齐了，`make` 那层「docker 包装」是给开发者裸机用的。

#### 4.3.2 核心流程

CI 在 push 到 `main`（或 `releases/**`）时的执行序列：

1. **触发**：GitHub 检测到 `push` 到 `main`（或 `releases/**` 分支），也接受到这些分支的 `pull_request`。
2. **Lint job**：在普通 `ubuntu-latest` 上 checkout 代码（一个轻量冒烟检查，确保仓库可检出）。
3. **Build job**（依赖 Lint 成功）：在容器 `fangruil/chisel-dev:amd64` 里跑 `sbt run`——即 elaborate `top.Main` 生成 `top.sv`，验证「设计至少能综合出来」。
4. **Test job**（依赖 Build 成功）：在同一容器里跑 `sbt test`——运行全部 20 个 Spec。
5. 三个 job 由 `needs` 串成链：Lint → Build → Test，前一个失败后一个就不跑，实现「失败快速止损」。

```
push/PR 到 main 或 releases/**
        │
        ▼
   ┌────────┐  needs   ┌────────┐  needs   ┌────────┐
   │  Lint  │ ───────▶ │ Build  │ ───────▶ │  Test  │
   │ checkout│          │sbt run │          │sbt test│
   │ (无容器) │          │(chisel │          │(chisel │
   └────────┘           │ -dev容器)│          │ -dev容器)│
                        └────────┘          └────────┘
```

一个设计要点：Build 先于 Test。这样如果改动把 elaborate 弄坏了（比如参数非法、Chisel 语法错），会在 Build 阶段快速失败，不必浪费时间去编译和跑那些注定跑不通的测试。

#### 4.3.3 源码精读

[.github/workflows/actions.yml:1-10](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L1-L10) — 工作流名 `Chisel CI`；触发条件 `on: push` 到 `main` 与 `releases/**`，以及到这些分支的 `pull_request`。这意味着往 `main` 推、或在 PR 里改代码都会触发 CI。

[.github/workflows/actions.yml:12-17](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L12-L17) — **Lint job**：跑在 `ubuntu-latest`，没有 `container` 字段，唯一的步骤是 `actions/checkout@v4`。它不构建也不测试，只是一个「仓库能被检出」的冒烟关卡。

[.github/workflows/actions.yml:19-27](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L19-L27) — **Build job**：`needs: Lint`（Lint 过了才跑）；`container.image: fangruil/chisel-dev:amd64`（在项目自带镜像里跑）；步骤为 checkout 后执行 `sbt run`。这一步把 `top.Main` 跑出来、生成 `top.sv`，验证设计能 elaborate。

[.github/workflows/actions.yml:29-37](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L29-L37) — **Test job**：`needs: Build`（Build 过了才跑）；同样在 `fangruil/chisel-dev:amd64` 容器里；步骤为 checkout 后执行 `sbt test`，运行全部 Spec。

注意 CI 与本地的差异：CI 在容器内**直接** `sbt run` / `sbt test`；而本地的 `make test` / `make build` 是在宿主机上用 `docker run` 把 sbt 包一层（见 [Makefile:27-28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L27-L28)）。这是因为 CI 的 job 本身就已经跑在那个容器里，不必再嵌一层；本地开发者则用 make 自动套上 docker。两条路径最终执行的 sbt 命令一致，所以「CI 能过、本地也能过」的可复现性由此保证。

[AGENTS.md:130-132](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L130-L132) — AGENTS.md 的 CI 段落把这条流水线总结为一句话：在 push/PR 到 `main` 或 `releases/**` 时，于 `fangruil/chisel-dev:amd64` 内先 `sbt run` 后 `sbt test`，并提示用 `make container` + `sbt run`/`sbt test` 本地复现失败。

#### 4.3.4 代码实践

**实践目标**：逐行解释 CI 在 push 到 `main` 时执行的确切命令序列。

1. 打开 `.github/workflows/actions.yml`。
2. 假设你刚 `git push origin main`，按 job 顺序写下 GitHub Actions 实际执行的命令。
3. 对每个 job，标明：运行环境（是否有 `container`）、依赖哪个前置 job、执行的 shell 命令。
4. **需要观察的现象**：你能用一张表完整还原命令序列，不依赖任何额外文档。
5. **预期结果**：

   | 顺序 | Job | 依赖 | 运行环境 | 关键命令 |
   |:---:|:---|:---|:---|:---|
   | 1 | Lint | 无 | `ubuntu-latest`（无容器） | `actions/checkout@v4` |
   | 2 | Build | Lint | 容器 `fangruil/chisel-dev:amd64` | `sbt run` |
   | 3 | Test | Build | 容器 `fangruil/chisel-dev:amd64` | `sbt test` |

6. 若想本地复现某个失败：`make container` 进入镜像交互式 shell，再手动跑 `sbt run` 或 `sbt test`（与 CI 同环境）。

#### 4.3.5 小练习与答案

**练习 1**：如果你的改动只动了测试文件、没动 RTL，CI 仍会跑 `sbt run`（Build job）吗？为什么？

<details><summary>参考答案</summary>

会。CI 没有按「是否改动 RTL」做条件判断，只要触发条件（push/PR 到 `main`/`releases/**`）满足，Lint→Build→Test 三步都会跑。Build 的 `sbt run` 仍会执行 elaborate。这是一种「不聪明的全量验证」：牺牲一点时间换取「绝不漏检」的确定性。
</details>

**练习 2**：为什么 CI 在容器里直接 `sbt test`，而本地却要 `make test`？

<details><summary>参考答案</summary>

因为 CI 的 job 通过 `container.image: fangruil/chisel-dev:amd64` 直接运行在该镜像内，firtool/verilator/Chisel 已经备好，sbt 可以裸跑；而本地开发者的宿主机通常没有这些工具，所以 `make test` 用 `docker run ... sbt test` 在宿主机上自动套一层容器（见 Makefile）。两者执行的 sbt 命令相同，只是「套不套 docker」的差别，保证环境一致。
</details>

## 5. 综合实践

把本讲三个模块串起来，完成一次「模拟 CI」的全流程演练。

**任务**：假设你刚改动了 `NCoreBackendQuantSpec`，准备 push。请按 CI 的逻辑，先在本地用单测脚本快速验证，再说明远端 CI 会如何继续。

1. **先只跑改动相关的单测**（模块 4.2）：
   ```
   tool/test-specific-spec.sh backend.NCoreBackendQuantSpec
   ```
   观察 `-oDT` 输出的耗时，确认那条 `simulate(new NCoreBackend(...))` 是该 Spec 里最慢的用例之一。
2. **对照耗时，反思分层**（模块 4.1）：写下「为什么这条集成测试单次 simulate 比 `InstrDecoderSpec` 的单次 simulate 慢」，用「DUT 规模 = 完整后端 vs 单个组合模块 + EphemeralSimulator 现场编译」来解释。
3. **模拟 CI 全流程**（模块 4.3）：在本地 `make container` 进入镜像，依次执行 `sbt run`（对应 Build job）和 `sbt test`（对应 Test job），确认本地环境与 CI 一致。
4. **写出 push 到 `main` 后 CI 的确切序列**：Lint（checkout）→ Build（容器内 `sbt run`）→ Test（容器内 `sbt test`），三步串行、前缀失败即止。
5. **需要观察的现象**：本地单测先快速反馈；本地 `sbt run`+`sbt test` 与远端 CI 行为一致；耗时排序符合「集成 > 单元」。
6. **预期结果**：你能独立解释「为什么先单测、再全套、为什么 CI 要 Build 在 Test 前」，并能复现 CI 的命令序列。若本机无 Docker，第 3 步标「待本地验证」，但第 4 步的序列说明可不依赖运行直接完成。

## 6. 本讲小结

- `src/test/scala` 与 `src/main/scala` **镜像组织**：把 `main` 换成 `test`、文件名换成 `<模块>Spec.scala` 即可定位测试；`utils/` 放共享工具（非 Spec），`top/` 无测试。
- 测试分**单元层**（单模块：PE/VALU/译码器/寄存器堆/MMALU）与**集成层**（`backend/` 下的 `NCoreBackend*Spec`，测完整后端）。
- `tool/test-specific-spec.sh <全限定类名>` 只跑一个 Spec（内部是 `sbt "testOnly $1"` 在 `fangruil/chisel-dev:amd64` 里跑）；`tool/test-all.sh` 跑全部。
- `build.sbt` 的 `-oDT` 选项按耗时从慢到快打印每个用例，是对比单测/集成测试耗时的数据来源；集成测试慢的根因是 EphemeralSimulator 对更大的 DUT 要现场 elaborate+编译。
- CI（`.github/workflows/actions.yml`）在 push/PR 到 `main` 或 `releases/**` 时，串行跑 Lint（checkout）→ Build（容器内 `sbt run`）→ Test（容器内 `sbt test`），前一个失败后一个不跑。
- CI 在容器内**直接**调 sbt，本地则用 `make` 套一层 docker——两条路径执行的 sbt 命令一致，保证可复现。

## 7. 下一步学习建议

- **回到端到端语义**：本讲只看了 `NCoreBackendQuantSpec` 的「外壳」（怎么组织、怎么跑）。若想理解它内部七步量化流水线的真正含义，去读 u7-l1（后量化流水线）与 u7-l2（GEMM+Softmax），那里的数值细节会反过来让你看懂这个集成测试在断言什么。
- **补齐 Chisel 6 仿真的底层**：如果对「为什么 `expect` ChiselEnum 不合法」「为什么每个 simulate 都要重新编译」还有疑问，复习 u9-l1。
- **动手扩展测试**：挑一个目前只有单元测试、没有集成覆盖的场景（例如给 `MultiWidthRegisterBlock` 写一个与 VALU 联动的 backend 级用例），按本讲的「镜像 + 分层」约定放置文件，用 `test-specific-spec.sh` 调试，最后观察 CI 是否在你的 PR 上自动跑通——这是把本讲知识变成肌肉记忆的最快路径。
