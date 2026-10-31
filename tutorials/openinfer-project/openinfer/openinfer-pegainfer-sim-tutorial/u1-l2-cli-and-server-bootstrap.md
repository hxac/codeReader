# u1-l2 CLI 与服务器引导:legacy 定时模式与 --profile 档案模式

## 1. 本讲目标

上一讲我们从整体上认识了 pegainfer-sim:一个纯 CPU 的"假"推理引擎,用来在没有 GPU 的环境下验证 vLLM 兼容前端。本讲我们精读它的二进制入口 `src/main.rs`,学完后你应该能够:

1. 说出 **legacy 定时模式**与 **--profile 档案模式**各自的参数集,以及每个参数在用户未提供时的默认值(`DEFAULT_*` 常量)。
2. 解释 `--profile` 与三个定时参数互斥的防御逻辑:它检查什么、为什么放在最前面、报错文案长什么样。
3. 完整跟踪 `main()` 的启动路径:从 clap 参数解析 → `build_runtime` 组装配置 → `start_engine` 装配引擎 → `pegainfer_frontend::vllm::serve` 把引擎挂到 OpenAI 兼容的 HTTP 服务上,并把 `serve` 的 8 个参数一一对应到来源。

`main.rs` 只有约 260 行,但它是"两条车道"在进程层面的分岔点,读懂它,后面单元的所有内容都有了落脚处。

## 2. 前置知识

本讲会用到以下几个 Rust 生态的基础概念,先通俗地过一遍:

- **clap derive 宏**:`#[derive(Parser)]` 让一个结构体变成命令行参数的定义。Rust 字段名的 snake_case 会自动映射成 CLI 的 kebab-case,例如字段 `base_ttft_ms` 对应选项 `--base-ttft-ms`。字段上方 `///` 文档注释会变成 `--help` 里的参数说明。
- **两种"默认值"策略**:
  - `#[arg(long, default_value_t = 8000)]`:clap 层默认值,字段类型是具体类型(如 `u16`),程序里拿到的**永远有值**,无法区分"用户传了 8000"和"没传"。
  - 字段类型是 `Option<f64>`、代码里 `unwrap_or(DEFAULT_X)`:代码层默认值,**能区分"用户给没给"**。本讲的互斥校验正是靠这个区别实现的——只有 `Option::is_some()` 才知道用户显式提供了参数。
- **anyhow 三件套**(本 crate 统一用 `anyhow` 做错误处理):
  - `ensure!(cond, "msg")`:条件不成立时返回错误 `msg`;
  - `bail!("msg")`:无条件返回错误(等价于 `ensure!(false, ...)`);
  - `.with_context(|| "msg")`:给 `Result` 的错误包一层上下文,最终错误链会显示成 `msg: 底层错误`。
- **`std::sync::Once`**:保证一段代码在整个进程里只执行一次。日志系统全局只能安装一次,重复安装会 panic,所以用 `Once` 防护——测试代码会多次调用 `init_logging()`,这个防护是必需的。
- **承接 u1-l1 的认知**:pegainfer-sim 有两条互斥的调度车道——legacy 车道用固定 TTFT/TPOT 定时;`--profile` 车道由校准档案(JSON)决定调度与步计时。本讲讲的就是这两条车道在 CLI 层的分岔。

其中 TTFT(首 token 延迟)在 legacy 车道里由两个参数共同决定:

\[ \text{TTFT} = \texttt{base\_ttft\_ms} + \frac{\texttt{prompt\_len}}{\texttt{prefill\_tokens\_per\_ms}} \]

即"固定起步开销 + 按 prefill 吞吐折算的 prompt 处理时间"。TPOT 则是每个生成 token 之间的固定间隔 `tpot_ms`。这两个公式的完整执行语义在 u2-l1 展开,本讲只需要理解参数含义。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲涉及程度 |
|------|------|------------|
| [pegainfer-sim/src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs) | 二进制入口:CLI 定义、两种模式的组装、启动前端 | **本讲主角,逐段精读** |
| [pegainfer-sim/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs) | `SimulatedEngineConfig::new` / `with_*` builder / `start_engine` | 只看签名,下一讲(u1-l3)深入 |
| [pegainfer-sim/src/profile.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs) | `EngineProfile::load_from_path` 与 `LoadedEngineProfile` | 只看加载入口,单元三深入 |
| [pegainfer-frontend/src/vllm/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/mod.rs) | `serve` 的完整签名、`shutdown_token_from_ctrl_c` | 看 `main()` 最后一个调用的契约 |
| [pegainfer-sim/tests/frontend_e2e.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs) | e2e 测试:`spawn_once` 是 `main()` 的测试侧样板;`TINY_*` 常量提供最小模型元数据 | 实践环节取材 |
| [pegainfer-sim/tests/fixtures/](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-zero-cost.json) | `online-zero-cost.json` 等校准档案 bundle | 实践环节的 `--profile` 输入 |

## 4. 核心概念与源码讲解

### 4.1 Args:clap 参数定义与两条车道的参数集

#### 4.1.1 概念说明

`Args` 结构体是整个 CLI 的单一事实来源。理解它的关键是把 8 个参数按"哪条车道用它"分成三组:

- **通用参数**:`model_id`、`model_path`、`port`、`fallback_token_id`、`profile`——两条车道都认,但**语义**可能随车道变化(例如 `model_id` 在 legacy 模式下还兼作元数据目录路径的默认值,在 profile 模式下则必须与档案声明的模型一致)。
- **legacy 车道定时参数**:`base_ttft_ms`、`prefill_tokens_per_ms`、`tpot_ms`——只在 legacy 车道有效,与 `--profile` 互斥。
- **半通用参数**:`max_model_len`——两条车道都接受,但 profile 车道把它当**校验项**(必须与档案一致),legacy 车道把它当**配置项**(直接采用)。

#### 4.1.2 核心流程

clap 拿到命令行后:

1. 把 `--model-id xxx` 之类的 kebab-case 选项匹配到对应字段;
2. 有 `default_value_t` 的字段直接填默认值(`port`、`fallback_token_id`);
3. 其余可选项不出现时保持 `None`;
4. 解析失败(类型不符、未知选项)时打印用法并退出。

随后 `Args` 被**原样传给** `build_runtime`,由后者——而不是 clap——来做跨参数的校验(互斥、与档案冲突)。这是一个值得注意的分层:clap 只管"单个参数的语法",业务约束全部集中在 `build_runtime`。

#### 4.1.3 源码精读

六个默认值常量集中定义在文件顶部,方便一眼扫出 legacy 车道的"出厂设置":

[pegainfer-sim/src/main.rs:L14-L19](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L14-L19) 定义了 `DEFAULT_MODEL_ID`("Qwen/Qwen3-0.6B")、`DEFAULT_MAX_MODEL_LEN`(8192)、`DEFAULT_BASE_TTFT_MS`(5.0ms)、`DEFAULT_PREFILL_TOKENS_PER_MS`(100.0)、`DEFAULT_TPOT_MS`(12.0ms)和 `DEFAULT_FALLBACK_TOKEN_ID`(0)。注意只有最后一个被 clap 直接引用,其余都是代码层默认。

[pegainfer-sim/src/main.rs:L23-L28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L23-L28) 是 derive 声明与 `about` 文案——`--help` 第一行就来自这里("CPU-only simulated inference server for OpenAI/vLLM serving benchmarks")。

[pegainfer-sim/src/main.rs:L29-L37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L29-L37) 定义 `model_id` 与 `model_path`,二者的文档注释明确写出了车道差异:`model_id` 在 legacy 模式"还兼作元数据路径",在 profile 模式"必须匹配档案的目标模型 id";`model_path` 在 profile 模式下"可以与档案的目标模型 id 不同"(即档案描述的是被校准的真实引擎,而本地 tokenizer 可以是另一个小模型)。

[pegainfer-sim/src/main.rs:L59-L65](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L59-L65) 展示了两种默认值策略的对比:`fallback_token_id` 用 `default_value_t`(clap 层默认,字段是具体的 `u32`),`profile` 是 `Option<PathBuf>`(是否提供 `--profile` 本身就是车道选择开关)。

#### 4.1.4 代码实践

1. **实践目标**:把 CLI 表面完整看一遍,验证参数名与文档注释的映射。
2. **操作步骤**:在仓库根目录运行 `cargo run --release -p pegainfer-sim -- --help`(注意工作区默认成员不含本 crate,`-p pegainfer-sim` 不能省)。
3. **需要观察的现象**:帮助文本里 8 个选项的名称与说明;`--port` 与 `--fallback-token-id` 显示各自的默认值 8000 与 0。
4. **预期结果**:能看到全部选项列表;再故意传一个未定义选项(如 `--tpot`),clap 会报 "unexpected argument" 并建议 `--tpot-ms`。
5. 以上均为 clap 的确定性行为;具体帮助文案排版**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:`base_ttft_ms` 为什么用 `Option<f64>` + 代码层默认,而不是像 `port` 那样用 `default_value_t`?

> **答案**:因为互斥校验需要区分"用户显式提供了 `--base-ttft-ms`"与"未提供"。`default_value_t` 会让字段永远有值,`is_some()` 永远为真,`ensure_legacy_timing_flags_are_absent` 就无法工作了。见 [main.rs:L156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L156)。

**练习 2**:legacy 车道下 TTFT 公式 \(\text{TTFT} = 5 + \frac{\text{prompt\_len}}{100}\) 毫秒,一个 2000 token 的 prompt 首延迟是多少?

> **答案**:\(5 + 2000/100 = 25\) 毫秒。`prefill_tokens_per_ms` 就是把 prompt 长度折算成毫秒的"吞吐"系数。

**练习 3**:`--profile` 指定的文件路径,与 `--model-path` 指定的目录,分别装着什么?

> **答案**:`--profile` 指向校准档案 JSON(版本化 timing + scheduler 画像,旁边还要有 manifest,见 u3-l1);`--model-path` 指向 tokenizer/模型元数据目录(`tokenizer.json`、`config.json` 等),是 vLLM 前端搭协议栈用的。

### 4.2 build_runtime:两条车道的分岔点

#### 4.2.1 概念说明

`build_runtime` 把 `Args` 翻译成一个 `RuntimeConfig`——引擎配置 + 前端服务所需的全部信息。它是整个 main.rs 里唯一有"业务逻辑"的函数,所有防御性校验都集中在这里。

`RuntimeConfig` 的五个字段([main.rs:L68-L75](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L68-L75)):`engine`(给 `start_engine` 的引擎配置)、`model_path`(前端元数据目录)、`served_model_name`(对外模型名列表)、`max_model_len`(报告给前端的上下文上限)、`profile`(加载好的档案,legacy 车道为 `None`)。

#### 4.2.2 核心流程

```text
build_runtime(args):
  if args.profile 存在:                      # ── profile 车道
      1. 定时参数必须全部缺席(否则 bail)
      2. load_from_path 加载档案(读 JSON + manifest + sha256 校验)
      3. 若给了 --model-id,必须等于档案 model_id
      4. 若给了 --max-model-len,必须等于档案 max_model_len
      5. engine = 默认配置 + fallback_token_id + 档案
      6. model_path 默认取 model_id 字符串
  else:                                       # ── legacy 车道
      1. model_id 三级回退:--model-id → --model-path 字符串 → DEFAULT_MODEL_ID
      2. model_path 默认 = model_id(作为路径)
      3. max_model_len 默认 8192,必须 > 0
      4. 三个定时参数各取默认值
      5. engine = SimulatedEngineConfig::new(四个值)
      6. served_model_name 仅当 --model-path 与 --model-id 同时给出才非空
```

#### 4.2.3 源码精读

**profile 车道**:[main.rs:L78-L81](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L78-L81) 是车道入口:先做互斥校验(注意顺序——**先于档案加载**,所以即使 profile 路径写错,定时参数冲突也会最先报出来),再调 `EngineProfile::load_from_path`([profile.rs:L48-L56](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L48-L56) 起读入 JSON 并顺着 `calibration.manifest` 相对路径找 manifest)。加载成功后,档案成为"模型身份"的权威:[main.rs:L83-L98](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L83-L98) 用两个 `ensure!` 保证 CLI 若显式给出 `--model-id` / `--max-model-len`,必须与档案完全一致,报错文案分别是 `--model-id '...' conflicts with profile model_id '...'` 和 `--max-model-len ... conflicts with profile max_model_len ...`——CLI 在这条车道上只能"确认",不能"改写"档案。[main.rs:L99-L113](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L99-L113) 组装引擎配置(从 `default()` 出发,只覆盖 `fallback_token_id` 和档案;`with_engine_profile` 内部还会再 `validate()` 一次,见 [lib.rs:L142-L146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L142-L146)),且 `max_model_len` 与 `served_model_name` 都取自档案——这条车道上**档案说什么就是什么**。

**legacy 车道**:[main.rs:L115-L127](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L115-L127) 是 `model_id` 的三级回退链(`--model-id` → `--model-path` 的字符串形式 → `DEFAULT_MODEL_ID`),`model_path` 则默认等于 `model_id`(这意味着**不带任何参数启动时,前端会去 `./Qwen/Qwen3-0.6B` 找 tokenizer**——目录不存在就起不来,综合实践会解决这一点)。[main.rs:L128-L140](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L128-L140) 校验 `max_model_len > 0`、填默认定时值并调 `SimulatedEngineConfig::new`([lib.rs:L69-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L69-L77) 起对每个值做有限非负校验)。

最微妙的一行在 [main.rs:L141-L151](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L141-L151):`served_model_name` 只有在 `--model-path` 与 `--model-id` **同时给出**时才填 `[model_id]`,否则是空 `Vec`。设计意图可以这样读:模型名与元数据目录路径不一致时才需要显式区分;空列表交给前端自行从 `model_path` 推导对外模型名。对照 profile 车道无条件填 `[model_id]`(L110),可见档案车道没有这种二义性。

#### 4.2.4 代码实践

1. **实践目标**:用源码推导 `served_model_name` 的取值,再用 `/v1/models` 实测。
2. **操作步骤**:分别以 (a) 只传 `--model-path /tmp/sim-model`、(b) 同时传 `--model-id my-sim --model-path /tmp/sim-model` 两种方式启动(目录构造见第 5 节),各请求一次 `/v1/models`。
3. **需要观察的现象**:两次响应里 `data[0].id` 的值。
4. **预期结果**:情形 (a) 的 id 与 `model_path` 字符串相关,情形 (b) 的 id 为 `my-sim`(served_model_name 非空生效)。具体格式**待本地验证**。
5. 本实践依赖第 5 节的最小模型目录,建议先完成综合实践再做。

#### 4.2.5 小练习与答案

**练习 1**:profile 车道下 `max_model_len` 从哪里来?用户传 `--max-model-len 4096` 会发生什么?

> **答案**:来自档案的 `profile.scheduler.max_model_len`(L111)。若显式传 4096,`ensure!` 会比较它与档案值,不等则启动失败(L91-L98);相等则正常启动(等于什么都没改)。

**练习 2**:为什么 profile 车道不需要 `ensure!(max_model_len > 0)` 这条校验?

> **答案**:档案在加载/验证时(`load_from_path` → `with_engine_profile` 内的 `validate()`)已经保证了自身一致性;legacy 车道的值直接来自自由的用户输入,所以需要单独防 `0`。

**练习 3**:legacy 车道下,`--model-path /tmp/m` 且不给 `--model-id`,`model_id` 最终是什么?`served_model_name` 呢?

> **答案**:`model_id` 回退为 `/tmp/m` 的字符串形式(L119-L121);`served_model_name` 为空 `Vec`(两个参数没有同时给出)。

### 4.3 ensure_legacy_timing_flags_are_absent:互斥防御

#### 4.3.1 概念说明

这条防线解决的问题是:**定时数据只能有一个真相**。profile 车道的步时长来自档案的校准测量(三线性插值,见 u3-l3),如果允许同时传 `--tpot-ms`,就会出现"档案说一步 8ms、CLI 说一步 12ms"的无声冲突——无论哪边赢,基准数据都可解释性尽失。所以选择 **fail-fast**:启动即拒绝,并解释原因。

#### 4.3.2 核心流程

```text
ensure_legacy_timing_flags_are_absent(args):
  flags = [("--base-ttft-ms",            是否提供),
           ("--prefill-tokens-per-ms",   是否提供),
           ("--tpot-ms",                 是否提供)]
  找到第一个"已提供"的 (name, true):
      bail!("{name} cannot be combined with --profile; timing comes from the profile")
  否则 Ok(())
```

用 `(名字, 是否提供)` 的数组字面量 + `find` 写校验,加新参数只需加一行元组,且报错信息天然带上**CLI 风格的参数名**。

#### 4.3.3 源码精读

[pegainfer-sim/src/main.rs:L154-L167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L154-L167) 是完整实现:`provided` 数组列出三个定时参数及其 `is_some()` 状态;`provided.into_iter().find(|(_, present)| *present)` 定位第一个被显式提供的参数;`if let Some((name, true))` 解构后 `bail!`。注意它的调用点在 [main.rs:L79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L79)——`build_runtime` 进入 profile 分支后的**第一件事**,先于任何文件 I/O:冲突是最便宜的失败,应最先暴露。

#### 4.3.4 代码实践

1. **实践目标**:亲手触发互斥报错,记住文案(它会在你以后误用命令行时立刻认出来)。
2. **操作步骤**:运行 `cargo run --release -p pegainfer-sim -- --profile tests/fixtures/online-zero-cost.json --tpot-ms 12`(在 pegainfer-sim 目录内,fixture 才在相对路径上)。
3. **需要观察的现象**:进程退出码非 0,stderr 打印错误信息。
4. **预期结果**:错误信息为 `--tpot-ms cannot be combined with --profile; timing comes from the profile`。再把 `--tpot-ms` 换成 `--base-ttft-ms 3` 与 `--prefill-tokens-per-ms 50` 各试一次,文案中的参数名随之变化。
5. 这是纯内存分支,行为确定;唯一**待本地验证**的是 anyhow 错误在终端上的确切排版。

#### 4.3.5 小练习与答案

**练习 1**:三个定时参数同时提供,报错信息提到几个?

> **答案**:只提到第一个(`--base-ttft-ms`)。`find` 只返回首个匹配——fail-fast 风格下修好一个错误自然会暴露下一个,一次全列反而稀释信息。

**练习 2**:这个校验为什么不用 clap 的 `conflicts_with` 声明来做?

> **答案**(开放讨论):clap 层的 `conflicts_with` 也能做,但这里互斥的语义是"定时真相属于档案"这一业务规则,与 `build_runtime` 里另外两个档案一致性校验(model_id、max_model_len)同属一层;集中在业务层让所有跨参数规则可在一处读到。(clap 与 anyhow 两种方案的报错时机与文案控制力也不同。)

**练习 3**:`--fallback-token-id` 也影响输出,为什么它不参与互斥?

> **答案**:它不属于"定时"——档案管的是调度与步时长,fallback token 是空 prompt 时的输出占位(输出内容层面的旋钮),与档案无冲突,所以两条车道共用(见 L100 显式传入)。

### 4.4 report_profile 与 init_logging:启动横幅与日志系统

#### 4.4.1 概念说明

启动时有两类输出,职责不同:

- **启动横幅**(`report_profile`,`eprintln!` 直写 stderr):不受日志级别过滤,永远打印。它的价值是基准测试留档——跑基准时把 stderr 存下来,就永久记录了"这次用的哪个档案、什么调度参数、覆盖域是什么"。
- **运行日志**(`logforth` 框架,受 `RUST_LOG` 过滤):分级(INFO/DEBUG/...)、按 target 过滤的常规日志。

#### 4.4.2 核心流程

`init_logging` 用 `Once` 保证全局只安装一次:读 `RUST_LOG` 环境变量(默认 `info`)→ 构造 EnvFilter → 给 logforth 挂一个 Stderr appender → apply。`report_profile` 则按车道打印:legacy 一行话;profile 一大行结构化字段(路径、manifest、sha256、目标引擎、版本、模型、GPU、调度参数、覆盖域)。

#### 4.4.3 源码精读

[pegainfer-sim/src/main.rs:L169-L173](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L169-L173) 是 legacy 车道的横幅:一行 `active engine profile: legacy fixed TTFT/TPOT scheduler`——这条信息量虽小,但让日志读者立刻知道当前在快车道。

[pegainfer-sim/src/main.rs:L174-L190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L174-L190) 是 profile 车道的横幅,字段全部来自已加载的 `LoadedEngineProfile`(该结构在 [profile.rs:L180-L186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L180-L186) 定义,含 `profile`、`manifest` 与两个路径字段,并 `Deref` 到 `EngineProfile`):`path`/`manifest`/`manifest_sha256` 三元组锁定数据来源,`target`/`version`/`model`/`revision`/`gpu` 描述被校准的真实引擎,`scheduler={:?}` 及三个调度参数、`predictor_coverage={:?}` 则是调度与计时的关键输入——这些概念在单元三逐一展开。

[pegainfer-sim/src/main.rs:L193-L205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L193-L205) 是 `init_logging` 全文:`LOGGING_INIT.call_once`([L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L21) 定义)包裹 `EnvFilterBuilder::from_spec(...)` 到 `.apply()` 的安装流程。单元测试 [main.rs:L253-L257](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L253-L257) 的 `sim_logger_accepts_warn_records` 正是靠 `Once` 才能在测试里安全地再次调用 `init_logging()` 并断言 Warn 级别对 `pegainfer_sim::profile` target 可见。

#### 4.4.4 代码实践

1. **实践目标**:观察横幅与日志两套输出的分离。
2. **操作步骤**:先默认启动(需要第 5 节的模型目录),记录 stderr 前几行;再用 `RUST_LOG=pegainfer_sim=debug cargo run --release -p pegainfer-sim -- ...` 启动一次。
3. **需要观察的现象**:横幅两轮都在(不受 RUST_LOG 影响);第二轮多出 `pegainfer_sim` target 的 debug 日志。
4. **预期结果**:横幅行首为 `active engine profile: legacy fixed TTFT/TPOT scheduler`。debug 日志的具体条数与内容**待本地验证**。
5. 若想验证 `Once`,可写个小测试连续调用 `init_logging()` 两次,确认不 panic。

#### 4.4.5 小练习与答案

**练习 1**:横幅为什么用 `eprintln!` 而不是 `log::info!`?

> **答案**:`log::info!` 受 `RUST_LOG` 过滤,级别调高(如 `RUST_LOG=warn`)时横幅会消失;而"这次跑的是哪个档案"是基准记录的硬需求,必须无条件出现,所以直写 stderr。

**练习 2**:`RUST_LOG` 未设置时默认 spec 是什么?

> **答案**:`"info"`(L195 的 `unwrap_or_else`)。即默认能看到 info 及以上级别的日志。

**练习 3**:profile 横幅里的 `manifest_sha256` 校验的是哪个文件?

> **答案**:manifest 中记录的 profile 文件内容摘要——`load_from_path` 在加载时已用它确认档案没有被改动过(细节在 u3-l1)。

### 4.5 main():从参数到 vllm::serve 的完整启动路径

#### 4.5.1 概念说明

`main` 把前四个模块串成一条线,终点是前端的 `serve`。`serve` 的第一个参数值得单独理解:它要的不是引擎,而是**一个产出引擎的 Future**——这允许真实引擎做耗时的异步初始化(加载权重等);sim 引擎是同步构造好的,所以用 `std::future::ready(Ok(engine.into()))` 包成"立即就绪"的 future。这是 sim 与真实引擎在启动契约上的唯一差异。

#### 4.5.2 核心流程

```text
main():
  1. init_logging()                 # 装日志(Once)
  2. Args::parse()                  # clap 解析(语法层)
  3. build_runtime(&args)?          # 业务校验 + 组装 RuntimeConfig
  4. report_profile(&runtime)       # 打启动横幅(stderr)
  5. start_engine(&runtime.engine)  # 装配 Engine(起调度线程,下一讲)
  6. vllm::serve(...).await         # 挂 OpenAI 兼容 HTTP,阻塞到停机
```

任何一步失败(`?`)进程即退出,错误链由 anyhow 打印——步骤 3 的三类校验(互斥、model_id 冲突、max_model_len 冲突)都发生在这里。

#### 4.5.3 源码精读

[pegainfer-sim/src/main.rs:L207-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L207-L225) 是 `main` 全文,8 个 `serve` 实参与 `RuntimeConfig`/`Args` 的对应关系:

| 实参 | 来源 | 说明 |
|------|------|------|
| `std::future::ready(Ok(engine.into()))` | 第 5 步的 `engine` | 立即就绪的引擎 future;`into()` 转为前端的 `LaunchedEngine` |
| `&runtime.model_path` | `build_runtime` | tokenizer/config 元数据目录 |
| `runtime.served_model_name` | `build_runtime` | 对外模型名(空 Vec 时前端自行推导) |
| `ParserSelection::Auto` | 常量 | 工具调用解析器自动选择(按模型路径,见 [model_line.rs:L314-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L314-L321) 的解析逻辑;u4-l3 详解) |
| `args.port` | CLI | 监听端口,默认 8000 |
| `Some(runtime.max_model_len)` | `build_runtime` | legacy: CLI/默认 8192;profile: 档案值 |
| `shutdown_token_from_ctrl_c()` | [vllm/mod.rs:L557-L561](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/mod.rs#L557-L561) | Ctrl-C 触发优雅停机的 CancellationToken |

`serve` 的签名见 [pegainfer-frontend/src/vllm/mod.rs:L74-L82](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/mod.rs#L74-L82),它内部委托给 `serve_with_engine_count`(默认引擎数 1)。第 5 步的 `start_engine` 定义在 [lib.rs:L174-L176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L174-L176),是 `start_engine_with_partitions(config, 1)` 的别名——分区与调度线程模型是下一讲(u1-l3)的主题。

这套启动路径在测试里有逐行对应的样板:[tests/frontend_e2e.rs:L199-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L199-L225) 的 `spawn_once` 同样是 `start_engine_with_partitions(...)` → `serve_with_engine_count(std::future::ready(Ok(engine.into())), ...)`,区别只有两点:测试用**显式** `CancellationToken`(可编程触发停机)而非 Ctrl-C;`max_model_len` 固定传 `Some(128)`(配合 tiny config 的 `max_position_embeddings`)。读懂了 `main`,你就同时读懂了所有 e2e 测试的服务器启动半边。

#### 4.5.4 代码实践

1. **实践目标**:对照源码走一遍"从进程启动到 HTTP 监听"的时序,确认第 5、6 步的先后关系。
2. **操作步骤**:在 `serve` 调用前后各加一行 `eprintln!`(仅用于本地实验,**不要提交**),重新运行观察输出顺序;或直接运行综合实践的第 2 步,观察横幅出现与端口可访问的先后。
3. **需要观察的现象**:横幅(第 4 步)先于任何 HTTP 可达;Ctrl-C 后进程干净退出(第 6 步的 shutdown token 生效)。
4. **预期结果**:横幅 → 服务监听;Ctrl-C 一次即优雅退出(不是被 SIGINT 硬杀)。
5. Ctrl-C 的确切收尾日志**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**:`serve` 的第一个参数为什么是 Future 而不是 `Engine`?

> **答案**:给异步初始化的引擎留出契约空间(权重加载可能几十秒)。sim 引擎同步构造,用 `std::future::ready` 立即满足;真实引擎可以在 future 里慢慢初始化,前端在等待期间可以继续做自己的准备。

**练习 2**:`Some(runtime.max_model_len)` 里这个 `Option` 表达什么?

> **答案**:对前端而言 `None` 意味着"由前端/模型配置自行决定上下文上限"。sim 永远显式给值(legacy 8192 或档案值),把决定权握在自己手里,保证报告给 `/v1/models` 的元数据与调度上限一致。

**练习 3**:e2e 测试为什么不用 `shutdown_token_from_ctrl_c()`?

> **答案**:测试进程里 Ctrl-C 不可编程控制;测试用自建的 `CancellationToken` 克隆一份传给 serve,`server.shutdown()` 时 cancel 即可确定性停机(`spawn_once` L207 的 `shutdown` 与 L209 的 `server_shutdown` 就是这一对克隆)。

## 5. 综合实践

把本讲的实践任务完整走一遍:启动服务器、验证三个端点、触发互斥报错。

**(1) 构造最小模型元数据目录**

前端搭 vLLM 协议栈需要 `tokenizer.json`、`tokenizer_config.json`、`config.json` 三个文件(e2e 测试的 [model_dir_with_minimal_metadata](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L373-L390) 就是这么写的,注释说明:token-id prompt 免编码,但**生成的 id 仍需 tokenizer 解码**)。在 pegainfer-sim 目录下建 `tmp-model/`,放入三个文件——`tokenizer.json` 直接照抄 [TINY_TOKENIZER_JSON](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L2106-L2136)(WordLevel 词表 `{"<unk>": 0, "alpha": 1, "beta": 2}`),`tokenizer_config.json` 照抄 [L2138-L2142](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L2138-L2142),`config.json` 照抄 [TINY_CONFIG_JSON](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L2153-L2157)。

**(2) 构建并启动**

```bash
cargo build --release -p pegainfer-sim
./target/release/pegainfer-sim --port 8001 --model-path tmp-model
```

预期 stderr 首行为 `active engine profile: legacy fixed TTFT/TPOT scheduler`,随后是前端监听日志(排版待本地验证)。

**(3) 三个端点逐一试**

```bash
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8001/v1/models
curl -s http://127.0.0.1:8001/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"tmp-model","prompt":[1,2],"max_tokens":4}'
```

- `/health`:预期成功(2xx)。响应体格式待本地验证。
- `/v1/models`:预期 `object: "list"`;`data[0].id` 与 `model_path` 推导出的模型名相关(本例未同时给 `--model-id`,`served_model_name` 为空,由前端推导)。
- `/v1/completions`:token id `[1, 2]` 经词表解码为 `alpha beta`(id→词的映射即 tokenizer.json 的 vocab);生成的 4 个 token 均为 fallback id 0(`<unk>`)。可断言:`finish_reason == "length"`、`usage.prompt_tokens == 2`、`usage.completion_tokens == 4`、响应耗时约 \(5 + 2/100 + 3 \times 12 \approx 41\) 毫秒量级(首 token TTFT + 后续 3 个间隔;网络开销另计)。生成 text 的确切拼接(是否带空格)**待本地验证**。

**(4) 触发互斥报错**

```bash
./target/release/pegainfer-sim \
  --profile tests/fixtures/online-zero-cost.json --tpot-ms 12
```

预期:进程立即退出,stderr 含 `--tpot-ms cannot be combined with --profile; timing comes from the profile`。对照 4.3 节源码,理解这句文案的两个成分:哪个参数非法(`{name}`)+ 为什么("timing comes from the profile")。

**(5) 反向确认 profile 车道可用**(选做)

去掉 `--tpot-ms` 再跑第 4 步的命令,并为档案模型准备元数据目录:`--model-path tmp-model`(档案 `model_id` 为 `pegainfer-sim-zero-cost`,见 [tests/fixtures/online-zero-cost.manifest.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-zero-cost.manifest.json),`model_path` 允许与之不同)。预期横幅变成 4.4 节描述的长行,`max_model_len` 等取档案值。档案车道的完整行为在 u3-l4 展开。

## 6. 本讲小结

- `Args` 用 `Option<T>` + 代码层默认(而非 `default_value_t`)承载三个定时参数,唯一目的就是让互斥校验能分辨"用户显式提供"。
- `build_runtime` 是两条车道的分岔点:profile 车道让档案成为模型身份与定时的唯一真相(CLI 只能确认不能改写),legacy 车道走 `model_id` 三级回退 + `DEFAULT_*` 常量。
- `ensure_legacy_timing_flags_are_absent` 用"数组 + find + bail"实现 fail-fast 互斥,且放在档案加载之前——最便宜的失败最先暴露;报错文案同时回答"哪个参数"和"为什么"。
- `report_profile` 是不受 `RUST_LOG` 影响的启动横幅,profile 车道打印的 sha256/调度参数/覆盖域是基准留档的依据;`init_logging` 用 `Once` 保证日志系统只装一次。
- `main()` 六步走:`init_logging → parse → build_runtime → report → start_engine → vllm::serve`;`serve` 收一个"产出引擎的 Future",sim 用 `std::future::ready` 立即满足——这正是模拟引擎与真实引擎在启动契约上的唯一差异,e2e 测试的 `spawn_once` 与之一一对应。

## 7. 下一步学习建议

本讲我们两次把 `start_engine` 一笔带过。下一讲 **u1-l3《引擎契约接入:SimulatedEngineConfig 与 start_engine》**就补上这块:`SimulatedEngineConfig::new` 与全部 `with_*` builder 旋钮(脚本化补全、假想投机解码、假想前缀缓存等)、`start_engine_with_partitions` 如何用 `spawn_scheduler` 为每个分区起调度线程并装配前端可用的 `Engine`。建议先动手做一遍本讲综合实践,再进入下一讲——之后你阅读的每个 `spawn_with_config` 测试都会立刻眼熟。
