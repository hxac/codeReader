# 测试套件组织与 CI 流水线

## 1. 本讲目标

上一讲(u9-l1)我们掌握了**怎么写**一个 Chisel 6 测试:`EphemeralSimulator`、`poke/step/peek/expect` 四原语、ChiselEnum 用 `peek().litValue` 间接比较、用 `NpuAssembler` 构造指令字。

本讲把视角从「单条测试」拉到「整个测试体系」和「自动化流水线」。读完本讲你应当能够:

1. 看懂 `src/test/scala` 下测试目录与 `src/main/scala` 源码目录的**镜像组织**,知道某个模块的测试该放哪、去哪找。
2. 区分**单元测试**(如 `InstrDecoderSpec`)与**集成测试**(如 `NCoreBackendQuantSpec`),理解二者在 elaborate(精细化)成本上的巨大差距。
3. 用 `tool/test-specific-spec.sh <全限定类名>` 只跑一个 Spec,并用 `-oDT` 输出对比单测与集成测试的耗时。
4. 逐行读懂 `.github/workflows/actions.yml`,说出 push 到 `main` 时 CI 执行的确切命令序列,并知道如何在本地复现 CI 失败。

## 2. 前置知识

- **sbt**:Scala 的构建工具,chisel-npu 用它编译 Scala/Chisel、跑测试。`sbt test` 跑全部测试,`sbt "testOnly <类名>"` 只跑指定类。
- **elaborate(精细化)成本**:上一讲讲过,`EphemeralSimulator` 是「用完即弃」的——每个 `simulate(new Module){...}` 都要现场把 Chisel 翻译成 FIRRTL、再经 `firtool` 编译成 Verilog、再由 verilator 编译成可仿真二进制。**DUT(被测设计)越大,这一次 elaborate 越慢**。这是本讲理解「单测快、集成测试慢」的根本原因。
- **AnyFlatSpec**:ScalaTest 提供的测试风格,`"X" should "Y" in { ... }` 是一个测试用例。chisel-npu 全部 Spec 都继承自它。
- **GitHub Actions**:GitHub 内置的 CI 服务,用一个 YAML 文件描述「什么事件触发、跑哪些步骤」。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/test/scala/**/*.scala` | 整个测试树,本讲关注它的**目录组织**而非单个实现 |
| `src/test/scala/isa/InstrDecoderSpec.scala` | 单元测试样板:只 elaborate 小模块、纯组合、多 `simulate` 块 |
| `src/test/scala/backend/NCoreBackendQuantSpec.scala` | 集成测试样板:elaborate 整个 `NCoreBackend`,把所有子用例**合并进一个** `simulate` 块以省 elaborate 成本 |
| `build.sbt` | 定义 `-oDT` 测试选项(按耗时排序打印每条用例) |
| `tool/test-specific-spec.sh` | 单测快捷脚本:`docker run ... sbt "testOnly $1"` |
| `tool/test-all.sh` | 等价于 `make test`:`docker run ... sbt test` |
| `.github/workflows/actions.yml` | CI 流水线定义:Lint→Build→Test 三段 |
| `AGENTS.md` | 仓库的「防猜错」说明,其中 `Single-test shortcut`(L24)与 `CI`(L130)两节是本讲的权威摘要 |
| `Makefile` | 本地开发的薄封装,把 sbt 命令包进 `docker run` |

## 4. 核心概念与源码讲解

### 4.1 测试目录与源码目录的镜像组织

#### 4.1.1 概念说明

chisel-npu 的测试代码**不是**随便堆在一个 `test/` 里,而是严格**镜像**`src/main/scala` 的目录结构:源码在 `src/main/scala/alu/vec/vec.scala`,它的测试就放在 `src/test/scala/alu/vec/` 下;源码在 `src/main/scala/backend/SimpleBackend.scala`,测试就在 `src/test/scala/backend/` 下。

这种镜像约定有两个好处:

- **可发现性**:想找某个模块的测试,直接把路径里的 `main` 换成 `test` 即可;反之看测试就知道它在测哪个源码模块。
- **包名一致性**:Scala 的 `package` 声明要和目录路径对应。镜像目录保证了被测类与测试类在同一个 Scala 包里,测试可以直接访问包内成员,无需跨包导入。

注意一个细节:`src/main/scala/top/top.scala`(顶层 elaborate 入口)和 `src/main/scala/utils/gates.scala`、`src/main/scala/sram/spm.scala`、`sreg.scala` 在测试侧**没有对应 Spec**——不是所有源码都必须有测试,但有的模块(如 `top`)本身只是组装、不值得单独测。

#### 4.1.2 核心流程

镜像目录下的文件分两类:

1. **Spec 类**:文件名以 `Spec` 结尾,含 `class XxxSpec extends AnyFlatSpec`,是真正的可执行测试。sbt 会自动发现并运行它们。
2. **共享工具**:不含 `Spec`,是被各 Spec `import` 的辅助代码。chisel-npu 把它们放在 `src/test/scala/utils/` 下,归入 `testUtil` 包。

按目录清点的镜像关系(只列子目录,展示「源码→测试」一一对应):

```
src/main/scala/                 src/test/scala/
├── alu/                         ├── alu/
│   ├── pe/   (procElem,basePE)  │   ├── pe/   (PESpec)
│   ├── vec/  (vec,fp)           │   ├── vec/  (VALU*Spec × 9)
│   └── mma/  (mma + sa/ + cu/)  │   └── mma/  (MMALU*Spec + sa/ + cu/)
├── backend/ (SimpleBackend)     ├── backend/ (NCoreBackend*Spec × 2)
├── isa/      (*Format/Decoder…)  ├── isa/      (InstrDecoderSpec)
├── sram/     (register/…)        ├── sram/     (*RegisterSpec × 2)
└── (utils, top: 无对应测试)       └── utils/    (共享工具,非 Spec)
```

#### 4.1.3 源码精读

整个测试树一眼可见其规模与组织:[src/test/scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala) 目录下按 `alu / backend / isa / sram / utils` 五大子目录铺开,与源码侧一一对应。

两个样板 Spec 的包声明印证了镜像约定:

- [src/test/scala/isa/InstrDecoderSpec.scala:L4-L4](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L4) —— `package isa`,与被测的 `src/main/scala/isa/instrDecoder.scala` 同包。
- [src/test/scala/backend/NCoreBackendQuantSpec.scala:L11-L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L11) —— `package backend`,与 `src/main/scala/backend/SimpleBackend.scala` 同包。

而 `utils/` 下是工具不是测试:[src/test/scala/utils/printHelper.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/utils/printHelper.scala) 与 `widthHelper.scala` 归在 `testUtil` 包,供各 Spec 共享(上一讲已介绍)。

#### 4.1.4 代码实践

1. **实践目标**:亲手验证镜像约定,养成「源码↔测试」来回跳转的肌肉记忆。
2. **操作步骤**:
   - 在仓库里打开 `src/main/scala/alu/vec/vec.scala`(VALU 源码)。
   - 把路径中的 `main` 改成 `test`,进入 `src/test/scala/alu/vec/`,数一数这里有几个 `VALU*Spec`。
   - 再对 `src/main/scala/alu/mma/` → `src/test/scala/alu/mma/` 做同样练习,注意 `mma/` 下还有 `sa/`、`cu/` 子目录的镜像。
3. **需要观察的现象**:源码侧 `alu/vec/` 下只有 2 个 `.scala`(`vec.scala`、`fp.scala`),而测试侧 `alu/vec/` 下有 9 个 `VALU*Spec`——**测试粒度比源码更细**,一个源码模块可由多个 Spec 从不同角度覆盖。
4. **预期结果**:能说出 VALU 的 9 个测试分别覆盖算术(`VALUArithSpec`)、逻辑、归约、LUT、激活、广播、FP32、CVT 等子能力。
5. 命令耗时「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**:`src/main/scala/sram/` 下有 `register.scala` 和 `multiWidthRegister.scala`,测试侧应该有几个 Spec?分别叫什么?
**答案**:2 个——`src/test/scala/sram/RegisterSpec.scala`(测老的 `RegisterBlock`)和 `MultiWidthRegisterSpec.scala`(测 VX/VE/VR 别名的 `MultiWidthRegisterBlock`)。

**练习 2**:为什么 `src/test/scala/utils/printHelper.scala` 不会在 `sbt test` 里被当成一个测试用例跑?
**答案**:因为它的类名不以 `Spec` 结尾、也不 `extends AnyFlatSpec`,它只是 `testUtil` 包里的共享工具;sbt 只自动发现并运行符合约定的 Spec 类。

---

### 4.2 单元测试与集成测试的分层

#### 4.2.1 概念说明

chisel-npu 的测试自然分两层:

- **单元测试**:只 elaborate 一个**小模块**(如 `InstrDecoder`、单个 `PE`、`VALU`),用例小、跑得快,定位精准。它们直接 `poke` 模块的 `io` 端口或 `ctrl` bundle。
- **集成测试**:elaborate **多个模块连起来的整体**(如 `NCoreBackend` = 译码器 + 寄存器堆 + MMALU + VALU),验证它们协作的端到端行为。这类测试集中在 `src/test/scala/backend/`,以 `NCoreBackend*` 开头。

由于 `EphemeralSimulator` 每次 `simulate{}` 都要重新 elaborate + 编译,**集成测试的 DUT 远大于单元测试,elaborate 成本是主要的耗时来源**。这是本讲最重要的性能直觉,也直接决定了下一节「为什么要用单测快捷方式」。

#### 4.2.2 核心流程

对比两种 Spec 的 elaborate 规模:

| 维度 | `InstrDecoderSpec`(单元) | `NCoreBackendQuantSpec`(集成) |
|:---|:---|:---|
| DUT | `new InstrDecoder`(纯组合译码器) | `new NCoreBackend(K=8, N=8, 32)`(完整后端,含 8×8 MMALU) |
| 含寄存器/状态 | 无(组合逻辑) | 大量(寄存器堆 + PE 累加器 + 移位寄存器) |
| 用例驱动 | 直接 poke `io.instr`,`step(0)` 看组合输出 | 写寄存器堆、发指令、`step()` 推进多拍、回读结果 |
| elaborate 成本 | 极低 | 高(DUT 大几十倍) |
| 典型耗时 | 秒级 | 显著更长 |

集成测试为了对抗 elaborate 成本,有一个关键设计:**把所有子用例塞进同一个 `simulate{}` 块**,只为整个 `NCoreBackend` 付一次 elaborate 钱。单元测试则相反,每个用例自己开一个 `simulate{}` 也无所谓——因为它的 DUT 太小,elaborate 几乎免费。

#### 4.2.3 源码精读

**单元测试样板** —— `InstrDecoderSpec` 每个用例都独立 `simulate` 一个小译码器,且只做组合求值:

[src/test/scala/isa/InstrDecoderSpec.scala:L44-L50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50) —— 一个典型用例:新建译码器、用 `NpuAssembler` 构造指令、poke 进去。

[src/test/scala/isa/InstrDecoderSpec.scala:L25-L30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L25-L30) —— 用例的核心:`step(0)` 只求值组合逻辑(不推进寄存器),随即检查 `illegal` 标志。整个 Spec 有二十来个这样的小 `simulate` 块,因 DUT 极小而毫无压力。

**集成测试样板** —— `NCoreBackendQuantSpec` 把全部子用例合并进**唯一一个** `simulate` 块:

[src/test/scala/backend/NCoreBackendQuantSpec.scala:L142-L153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L142-L153) —— 只 `simulate(new NCoreBackend(K, N, 32))` **一次**,然后在里面依次调用 7 个 `runXxx` 子函数。每两个子用例之间用 `resetAddrs` 清零地址输入,防止状态串味。

[src/test/scala/backend/NCoreBackendQuantSpec.scala:L32-L43](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L32-L43) —— `resetAddrs` 在子用例间复位所有地址输入,这是「共用一个 DUT」必须付出的纪律。

此外该 Spec 还含一个**无 DUT 的纯 Scala 属性测试**(L157 起),完全绕开 elaborate,用 `FpRef` 参考模型校验量化数学——这是分层测试的另一种形态:把数值正确性与硬件仿真解耦。

> 关于「量化链每步结果落在哪类寄存器」的细节,属于 u7-l1 的内容,本讲不展开,只关注它的**测试工程结构**。

#### 4.2.4 代码实践

1. **实践目标**:体会「共用 DUT」这一集成测试优化为何必要。
2. **操作步骤**:
   - 打开 `NCoreBackendQuantSpec.scala`,确认 L142 的 `simulate(new NCoreBackend(...))` 只出现一次,L145–L151 的 7 个子用例都在它内部。
   - 在脑中做一个反事实推演:如果像 `InstrDecoderSpec` 那样给每个 `runXxx` 各开一个 `simulate(new NCoreBackend(...))`,会触发几次 elaborate?
3. **需要观察的现象**:每多一次 `simulate(new NCoreBackend(...))`,就要重新 elaborate + firtool + verilator 编译一次完整后端(8×8 PE 阵列 + VALU + 寄存器堆)。
4. **预期结果**:得出结论——7 次独立 elaborate 会让该 Spec 慢约一个数量级,所以作者把子用例合并;这是「DUT 越大、越要复用 elaborate」的通用策略。
5. 实际耗时「待本地验证」(见 4.3 的计时实践)。

#### 4.2.5 小练习与答案

**练习 1**:`InstrDecoderSpec` 里每个用例都新建一个 `simulate(new InstrDecoder){...}`,为什么这里不怕 elaborate 成本?
**答案**:因为 `InstrDecoder` 是纯组合的小模块,无寄存器、无阵列,elaborate 与编译都几乎瞬时;而可读性上每个用例独立、互不污染状态,反而更清晰。

**练习 2**:为什么 `NCoreBackendQuantSpec` 要专门写一个 `resetAddrs` 并在子用例间调用它?
**答案**:因为它让 7 个子用例共用同一个已 elaborate 的 DUT。前一个用例 poke 进去的地址/数据会残留在 DUT 的输入上,`resetAddrs` 把它们清零,避免「上一个用例的地址」污染下一个用例的行为。

---

### 4.3 tool/test-specific-spec.sh 单测快捷方式

#### 4.3.1 概念说明

开发时跑 `sbt test`(全量)要编译并运行全部 20 个 Spec,其中集成测试很慢。绝大多数时候你只改了一个模块,只想跑**那一个** Spec。chisel-npu 提供了 `tool/test-specific-spec.sh`,一行命令在 Docker 里只跑指定的 Spec。

关键点:它接收的是**全限定类名**(fully qualified name),即「包名.类名」。因为 sbt 的 `testOnly` 是按类名匹配的,而 Scala 的类名必须连同包名才能唯一确定。

#### 4.3.2 核心流程

脚本本体只有一行,就是把 `sbt "testOnly $1"` 装进和 CI 一致的 Docker 镜像里执行:

```
docker run --rm --env SBT_OPTS="-Xmx8G -Xss2M" \
  -v ${PWD}:/workspace/ fangruil/chisel-dev:amd64 \
  sbt "testOnly $1"
```

调用方式(注意全限定类名):

| 命令 | 跑什么 |
|:---|:---|
| `tool/test-specific-spec.sh isa.InstrDecoderSpec` | 只跑译码器单元测试 |
| `tool/test-specific-spec.sh backend.NCoreBackendQuantSpec` | 只跑量化集成测试 |
| `tool/test-all.sh`(或 `make test`) | 跑全部 Spec |

配合 `build.sbt` 里的一项设置,sbt 还会**按耗时降序**打印每条用例的耗时,让你一眼看到谁最慢:

[build.sbt:L25-L25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L25) —— `Test / testOptions += Tests.Argument(TestFrameworks.ScalaTest, "-oDT")`。其中 `-oDT` 让 ScalaTest 输出每条测试的耗时并排序(`D`=duration,`T`=按耗时排序),这正是本讲实践任务对比耗时的依据。

AGENTS.md 把这个脚本作为权威的「日常单测」入口记录在案:

[AGENTS.md:L24-L26](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L24-L26) —— 「Single-test shortcut」一节:`tool/test-specific-spec.sh <fully.qualified.Spec>` 等价于镜像内的 `sbt "testOnly <Spec>"`。

#### 4.3.3 源码精读

[tool/test-specific-spec.sh:L1-L1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-specific-spec.sh#L1) —— 整个脚本:用 `fangruil/chisel-dev:amd64` 镜像、把当前目录挂到 `/workspace`、设 `SBT_OPTS` 给 JVM 8G 堆内存,执行 `sbt "testOnly $1"`。`$1` 就是你传入的全限定类名。

对照 [tool/test-all.sh:L1-L1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-all.sh#L1) —— 几乎相同,只是把 `sbt "testOnly $1"` 换成 `sbt test`,所以它等价于 `make test`。

这两个脚本是 `Makefile` 的补充:`make test`(Makefile L27–L28)会跑全部测试,而你想只跑一个时就用 `test-specific-spec.sh`。

#### 4.3.4 代码实践

1. **实践目标**:对比单元测试与集成测试的实际耗时,亲眼验证 4.2 的 elaborate 成本论断。
2. **操作步骤**(在仓库根目录执行,首次会拉取/加载 Docker 镜像):
   ```bash
   tool/test-specific-spec.sh isa.InstrDecoderSpec
   tool/test-specific-spec.sh backend.NCoreBackendQuantSpec
   ```
3. **需要观察的现象**:命令结尾,ScalaTest 因 `-oDT` 会打印一段按耗时降序排列的用例列表。注意两个数字:(a) 该命令的**总墙钟时间**(从启动 sbt 到结束);(b) 各条用例的耗时。重点关注 sbt 启动后「Compiling / Elaborating」阶段在两条命令间的差异。
4. **预期结果**:`NCoreBackendQuantSpec` 的总时间显著大于 `InstrDecoderSpec`,差额主要来自 elaborate + 编译 `NCoreBackend`(8×8 MMALU)这一步,而非用例本身的逻辑。若你看到 `NCoreBackendQuantSpec` 把 7 个子用例合并在一个 `simulate` 里、整体却仍比译码器慢很多,就印证了「大 DUT 的 elaborate 是主要开销」。
5. 具体耗时数字「待本地验证」(取决于机器与首次编译缓存)。

#### 4.3.5 小练习与答案

**练习 1**:如果你写了一个新 Spec,包声明是 `package isa`,类名是 `MyDecoderSpec`,该用什么命令只跑它?
**答案**:`tool/test-specific-spec.sh isa.MyDecoderSpec`。必须带包名前缀 `isa.`,因为 sbt `testOnly` 按(全限定)类名匹配。

**练习 2**:`build.sbt` 里的 `-oDT` 去掉 `D` 或 `T` 分别会怎样?
**答案**:`-oT`(只排序不打印耗时)失去耗时数字、只剩排序;`-oD`(只打印耗时不排序)能看到每条用例耗时但输出顺序按运行顺序而非耗时顺序。两个字母合用才同时满足「打印耗时 + 按耗时排序」,最便于定位慢测试。

---

### 4.4 GitHub Actions CI 流水线(actions.yml)

#### 4.4.1 概念说明

chisel-npu 用一个 GitHub Actions 配置文件 `.github/workflows/actions.yml` 实现持续集成:每次往 `main`(或 `releases/**`)推送、或针对这些分支提 PR,都会自动在云端跑一遍「构建 + 测试」,失败则拦截合并。

它和本地开发的关系是:**CI 直接在 `fangruil/chisel-dev:amd64` 镜像里跑原生 sbt 命令,不走 Makefile**。Makefile 是给本地开发者把 sbt 包进 Docker 的便利层;CI 因为本身就指定了 `container:`,所以直接 `sbt run` / `sbt test`。两者用的是**同一个镜像**,因此「本地复现 CI」非常容易。

#### 4.4.2 核心流程

整条流水线分三个**串行** job,后一个依赖前一个成功:

```
触发(push/PR → main | releases/**)
        │
        ▼
  ① Lint   (ubuntu-latest, 仅 checkout —— 当前是个占位/门禁)
        │  needs: Lint
        ▼
  ② Build  (container: fangruil/chisel-dev:amd64, 跑 sbt run → elaborate 出 top.sv)
        │  needs: Build
        ▼
  ③ Test   (container: fangruil/chisel-dev:amd64, 跑 sbt test → 全部 20 个 Spec)
```

要点:

- **触发条件**:`push` 到 `main` 或 `releases/**` 分支;`pull_request` 针对 `main` 或 `releases/**`。两个事件都覆盖,保证 PR 合并前必跑。
- **串行依赖**:`Build needs: Lint`、`Test needs: Build`。Lint 不过就不构建,构建不过就不测试,节省资源。
- **镜像一致**:Build 与 Test 都用 `fangruil/chisel-dev:amd64`,与本地 `make container`/`tool/*.sh` 完全一致,内含 firtool 1.62.1、verilator、SystemC 与 `publishLocal` 过的 Chisel 6.7.0。
- **两条 sbt 命令**:`sbt run`(elaborate `top.Main` 产出 `top.sv`,验证至少能编译通过并生成 RTL)、`sbt test`(跑全部 Spec)。

#### 4.4.3 源码精读

[.github/workflows/actions.yml:L1-L10](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L1-L10) —— 文件名 `Chisel CI` 与触发器:`on: push` 到 `main`/`releases/**`,`pull_request` 同样针对这两个分支。

[.github/workflows/actions.yml:L13-L17](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L13-L17) —— `Lint` job:跑在 `ubuntu-latest`,唯一的 step 是 `actions/checkout@v4`。**注意它没有真正的 lint 命令**,当前更像一个「占位 + 串行门禁」,为后续接 scalafmt/lint 预留。

[.github/workflows/actions.yml:L19-L27](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L19-L27) —— `Build` job:`needs: Lint`、`container: fangruil/chisel-dev:amd64`,checkout 后执行 `sbt run`。

[.github/workflows/actions.yml:L29-L37](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L29-L37) —— `Test` job:`needs: Build`、同样用 `fangruil/chisel-dev:amd64` 镜像,执行 `sbt test`。

AGENTS.md 的 CI 段是对这份文件的精确人话摘要:

[AGENTS.md:L130-L132](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L130-L132) —— 「on push/PR to main or releases/**, runs `sbt run` then `sbt test` inside `fangruil/chisel-dev:amd64`. Reproduce failures locally with `make container` + `sbt run` / `sbt test`.」

#### 4.4.4 代码实践

1. **实践目标**:逐行说清 push 到 `main` 时云端到底跑了什么,并验证本地可复现。
2. **操作步骤**:
   - 读 `.github/workflows/actions.yml`,按 L1→L37 顺序写下确切命令序列(见下方预期结果)。
   - 本地复现:执行 `make container`(进入镜像交互式 shell),然后在 shell 里依次 `sbt run` 与 `sbt test`,观察与 CI 等价的输出。
3. **需要观察的现象**:CI 是把 `sbt run` 与 `sbt test` 放在**两个独立 job、两次独立 sbt 启动**里;而 `make container` 里你可以连续跑两条命令、共享一次 sbt 会话(更快)。注意这个差异。
4. **预期结果**:push 到 `main` 时 CI 的确切序列为——
   1. `Lint`:checkout 仓库(无实质 lint)。
   2. `Build`(若 Lint 过):在 `fangruil/chisel-dev:amd64` 中 `sbt run`。
   3. `Test`(若 Build 过):在同一镜像中 `sbt test`,跑全部 20 个 Spec。
   三个 job 串行,任一失败则整条流水线失败。
5. 本地复现的具体耗时「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**:为什么 CI 用两个独立 job 分别跑 `sbt run` 和 `sbt test`,而不是合在一个 job 里连跑?
**答案**:独立 job 好处是**失败定位与资源节省**——构建失败(编译/elaborate 出错)与测试失败(行为不对)在 GitHub UI 里分开展示,且 `Test needs: Build` 保证构建不过就不浪费资源跑测试。代价是两次 sbt 启动/JVM 预热与 Coursier 依赖解析,但在 CI 上这点开销远不如可观测性重要。

**练习 2**:CI 跑的是 `fangruil/chisel-dev:amd64`,你的本地机器是 Apple Silicon(arm64),CI 能复现吗?
**答案**:CI 写死了 `:amd64`,在云端 x86 runner 上一致;本地 arm64 若也跑 `:amd64` 会走 QEMU 模拟、偏慢。本地应优先用对应架构镜像——`make container` 会按 `uname -m` 自动选 `:arm64`/`:amd64`(见 Makefile 的 arch 分支),与 CI 用同一份 Dockerfile 构建,工具链版本一致,故行为可复现。

---

## 5. 综合实践

把本讲三个要点(镜像目录、单测快捷方式、CI 自动发现)串成一个**「新增一个最小 Spec 并确认它能被 CI 跑到」**的小任务:

1. **实践目标**:写一个极简 Spec,放对目录、用对包名,验证它既能被 `test-specific-spec.sh` 单跑、也会被 `sbt test`(即 CI 的 Test job)自动发现。
2. **操作步骤**:
   - 选一个已有源码模块,例如 `src/main/scala/utils/gates.scala` 里的某个简单门(如 `ORGate`)。
   - 在**镜像目录**下新建 `src/test/scala/utils/GatesSpec.scala`,顶部声明 `package utils`(若它原属别的包则与之对齐),写 `class GatesSpec extends AnyFlatSpec`,用 `EphemeralSimulator` 写一两个最小 `poke/peek/expect`。
   - 单跑:`tool/test-specific-spec.sh utils.GatesSpec`(若包名不是 `utils` 就换成实际包名),确认通过。
   - 全量:`make test`,在 `-oDT` 输出里找到你的 `GatesSpec`,确认它被自动纳入。
   - 推理 CI:由于 CI 的 Test job 跑的就是 `sbt test`,而 sbt 按约定自动发现所有 `*Spec`,你的新 Spec **无需改动 `actions.yml`** 就会被 CI 跑到——前提是它放在了 `src/test/scala/` 下且类名符合约定。
3. **需要观察的现象**:新 Spec 是否出现在 `sbt test` 的运行清单里;它的耗时与同目录其他 Spec 相比如何(验证「DUT 越小 elaborate 越快」)。
4. **预期结果**:新 Spec 被 `testOnly` 与全量 `test` 都能发现并执行;无需动 CI 配置即可被云端覆盖。
5. 运行结果「待本地验证」。

> 提示:本任务把「目录镜像约定(4.1)→ 单测快捷方式(4.3)→ CI 自动发现(4.4)」串成了闭环;它不要求改源码,只新增测试文件,符合 worker 守则(不修改源码)。

## 6. 本讲小结

- chisel-npu 的测试目录 `src/test/scala/` **严格镜像**源码目录 `src/main/scala/`(alu/backend/isa/sram/utils),换路径中的 `main` 为 `test` 即可定位;`utils/` 下放共享工具、其余 `*Spec` 才是真测试。
- 测试分**两层**:单元测试(如 `InstrDecoderSpec`,elaborate 小模块、多 `simulate` 块、组合求值)与集成测试(如 `NCoreBackendQuantSpec`,elaborate 整个 `NCoreBackend`)。
- 集成测试因 DUT 大、elaborate 贵,会把所有子用例**合并进一个 `simulate` 块**只为付一次 elaborate 钱,并用 `resetAddrs` 在子用例间清状态。
- `tool/test-specific-spec.sh <全限定类名>` 在 `fangruil/chisel-dev:amd64` 里跑 `sbt "testOnly <类名>"`,是日常只跑一个 Spec 的入口;`build.sbt` 的 `-oDT` 按耗时排序打印每条用例。
- CI 由 `.github/workflows/actions.yml` 定义:push/PR 到 `main`/`releases/**` 时,在 `fangruil/chisel-dev:amd64` 里**串行**跑 Lint→Build(`sbt run`)→Test(`sbt test`),与本地用同一镜像,可用 `make container` 复现。
- CI 直接跑原生 sbt、不经 Makefile;Makefile 与 `tool/*.sh` 是本地把 sbt 包进 Docker 的便利层。

## 7. 下一步学习建议

- **回看一条完整测试链**:挑 `NCoreBackendGemmSoftmaxSpec`(最大的集成测试),用本讲的「合并 simulate」视角重读,体会端到端 GEMM+Softmax 流水线如何被一条 Spec 覆盖(数值细节见 u7-l2)。
- **动手优化测试体验**:试着在 `actions.yml` 的 `Lint` job 里接入真正的 `scalafmtCheckAll`(若项目引入 scalafmt),把当前的占位门禁变成实质 lint;注意保持与本地 `make` 工作流一致。
- **进阶到平台测试**:本讲覆盖的是软件仿真层(Chisel→verilator)。若对上板验证感兴趣,可阅读 `tool/hw/` 下的 bringup 脚本(`bringup_full.sh`、`program_bitstream.sh` 等),那是 FPGA 硬件侧的「测试与烧录流水线」,与本讲的软件 CI 互补。
- 若尚未读 u9-l1,建议先回看,本讲的 `EphemeralSimulator`、`peek().litValue`、`NpuAssembler` 构字等基础均来自该讲。
