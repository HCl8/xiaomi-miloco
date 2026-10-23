# 一次性审查 runner —— 部署件

在自托管运行机上跑 PR 自动审查：启动器循环起一次性容器，容器里就是 GitHub 官方 runner
加审查环境；容器只有一个出口（宿主机），到别处都没有路由。

## 设计要点

- **容器由启动器起，不由任务起**——任务对容器形态没有话语权，提不了特权、挂不了宿主目录。
- **每个容器跑在自己的轻量 VM 里**（Apple `container` 的默认形态），任务之间、任务与宿主机之间独立内核。
- **出口只有宿主机**：容器接在 `--internal`（host-only）网络上，连公网、连别的内网主机都不通，
  **唯一能到的就是宿主机在容器网段上的那个地址**。所以 GitHub 流量和模型流量都必须经宿主机的代理出去。
  （正因为它够得着宿主机，才需要 `pf-setup.sh` 再收一道口 —— 见下节。）
- **真 key 不进容器**：模型真 key 只在宿主机网关 `gateway.py` 手里，容器拿到的是一把假钥匙。
- **模型真名也不进容器**：容器只发别名（`a` / `b`），网关按 `model_aliases` 换成真名。
  真名一旦以环境变量形式留在容器里，被 prompt injection 诱导的 agent 跑一句 `env`
  就能把它写进公开的 PR 评论。

## 目录

```
prreview/
├── prrlib.py               共用：日志配置、配置读取、来源网段判断、出口自检靶子的判据
│                             （最后这条也带一个 CLI，装机脚本 pf-setup.sh 调它，两边共用一份）
├── gateway.py              宿主机网关（一个进程两个端口）
│                             18080 按域名白名单转发 GitHub
│                             18081 把假钥匙换成真 key 后转发模型
├── launcher.py             启动器：池子 + 启动前自检 + 内存准入 + 定期清理
├── ghapp.py                GitHub App 凭据：私钥签 JWT → 换 installation token（也可单独跑来自检）
├── pull_image.py           从 ghcr 取镜像（public 包，不需要 registry 凭据）
├── pf-setup.sh             装宿主机侧出口过滤（容器只能到宿主机）
├── prreview.example.json   配置模板，拷成 prreview.json 再改（不含凭据）
├── probe.sh                隔离探针（可手工跑，也可以在 workflow 里跑）
├── install-launchd.sh      装三个 LaunchAgent（container 服务自启 + gateway + launcher）
├── secrets/                chmod 600，全部不进 git
│   ├── github-app.pem      GitHub App 私钥（推荐，见下「凭据来源」）
│   ├── github-token        长期凭据（不配 App 时才用）
│   ├── model-key           模型真 key（只在宿主机网关手里）
│   └── container-token     给容器的假钥匙
└── logs/
```

## 装法

0. **把本目录放到 `~/prreview/`**，再把 `prreview.example.json` 拷成 `prreview.json`、
   改掉里面的镜像引用与仓库名：
   ```bash
   mkdir -p ~/prreview && cp -R <本目录>/. ~/prreview/
   cp ~/prreview/prreview.example.json ~/prreview/prreview.json
   ```
   下面各步的路径都按 `~/prreview/` 写。

1. **装 Apple container**（需要管理员密码）：
   ```bash
   curl -fL -o /tmp/container.pkg \
     https://github.com/apple/container/releases/download/1.5.0/container-1.5.0-installer-signed.pkg
   sudo installer -pkg /tmp/container.pkg -target /
   container system start
   ```
   如果 buildkit 报 `Rosetta is not installed`，写 `~/.config/container/config.toml`：
   ```toml
   [build]
   rosetta = false
   ```
   然后 `container system stop && container system start`。

2. **建网络**：`container network create --internal --subnet 192.168.100.0/24 prr-int`

3. **放凭据**（`~/prreview/secrets/`，全部 `chmod 600`）：见上表。

4. **取镜像**：`python3 ~/prreview/pull_image.py`
   （包设成 public 时按匿名拉，不需要任何凭据。要钉版本优先用构建时按 commit 打的 tag，
   比 `:latest` 稳。）

5. **装服务**：`bash ~/prreview/install-launchd.sh`
   （这一步必须在装出口过滤**之前**：`pf-setup.sh` 的探针要连网关那两个端口，网关没起来
   时两个「应当能通」的靶子必然不通，会把一份正确的规则判成坏的。）

   顺带装上 `com.prreview.container-system`：Apple container 的服务不是登录项，机器重启
   或重新登录后不会自己回来，而启动器的出口自检要起容器，会一直报
   `XPC connection error` 并停掉待命容器。这条 agent 在登录时补一次 `container system start`。

6. **装出口过滤**（关键，见下节）：`bash ~/prreview/pf-setup.sh`
   然后 `bash ~/prreview/pf-setup.sh --persist`（macOS 开机不自动启用 pf）

   这一步之前启动器是**故意不干活**的：它起容器前会检查"容器还够不够得着宿主机上与本方案
   无关的端口"，够得着说明这层还没装，于是停掉待命容器、不再起新的并报警。

## 凭据来源（App 或长期 token，二选一）

仓库级的 runner 接口要仓库 Admin 权限。用长期 token 实现这一点，等于在这台机器上放一把
"能改整个仓库设置"的钥匙；换成 **GitHub App** 可以把口径收到仓库内、且钥匙一小时就失效。

配了 `github_app_id` 就走 App，否则读 `github_token_file`。**两条路不自动降级**：App 签不出来
就报错，免得私钥失效被"悄悄改用旧 token"一直掩盖。

建 App（网页操作，只能由账号持有人做）：

1. Settings → Developer settings → GitHub Apps → New。Homepage URL 必填，Webhook **不要**勾。
2. Repository permissions **只勾 `Administration: Read and write`**，其余保持 No access。
   不要勾 `Contents` / `Workflows` —— 少了这两项，这枚 token 推不了代码、也改不了流水线。
3. 记下 **App ID**；**Generate a private key**，把下载的 `.pem` 放到 `secrets/github-app.pem`（600）。
4. Install App → 选 Only select repositories → 勾目标仓。

配置里加三项（`github_app_installation_id` 可省，只有一处安装时自动发现）：

```json
"github_app_id": 12345,
"github_app_private_key_file": "~/prreview/secrets/github-app.pem",
```

装完先单独验一次，不用改启动器：

```bash
python3 ~/prreview/ghapp.py --app-id 12345 --key ~/prreview/secrets/github-app.pem \
  --check --repo owner/name
```

## 出口过滤的必要性

Apple container 的 `--internal`（hostOnly）网络**不封宿主机**——官方对 hostOnly 的定义就是
"同一子网内互通"，而宿主就是那个子网的网关。另外上游还有个 open bug（apple/container#2062）：
hostOnly 网络里出站 TCP 是否被转发，取决于宿主机的 `net.inet.ip.forwarding`。

从 container 的设置里关不掉（`NetworkMode` 只有 `nat` / `hostOnly` 两个值，没有网关/NAT 开关；
`--option mode=isolated` 会被接受但忽略）。所以唯一能收口的位置是宿主机。

`pf-setup.sh` 在宿主上装一层只匹配容器网络那一段的规则（网段是脚本的默认值，可用 `PRR_SUBNET` 覆盖）：

```
pass  quick proto udp from 192.168.100.0/24 port 68 to any port 67
pass  quick proto tcp from 192.168.100.0/24 to 192.168.100.1 port { 18080, 18081 } flags S/SA keep state
block drop quick from 192.168.100.0/24 to any
```

规则以「源 = 该网段」为条件，机器上其它容器在别的网段，**一条都不匹配**。
`launcher.py` 起 runner 之前会检查一次：自己在宿主上临时开一个端口，看容器能不能连上。连得上就拒绝干活。

## 已知缺口（别当已经解决）

- **出口过滤依赖 pf 一直在位**。规则本身由 `/etc/pf.anchors/prreview` 提供、`/etc/pf.conf` 引用，
  开机生效要装 `--persist` 那个 LaunchDaemon。`launcher.py` 有自检兜底（封不住就不起容器），
  但那是发现，不是阻止。
- **长期凭据的权限**：不配 App 时，`secrets/github-token` 的权限往往比需要的宽——仓库级 runner
  接口要求仓库 Admin，落到 token 上就是"能管这个仓的全部设置"。优先用下面的 App。
- **假钥匙会进容器记录**：`ANTHROPIC_API_KEY`（给容器的那把假钥匙）走环境变量，
  因而会出现在容器的 `config.json` 里。这条是有意接受的——它是我们自己造的、
  只在本地代理上有效，而代理本身还限了来源网段。真凭据一律不走环境变量。

## 登记凭据进容器的方式

一次性登记凭据不能随便传，以下四条路都不可行：

| 路径 | 不行的原因 |
|---|---|
| `container run ... --jitconfig <blob>` | 凭据进宿主机的进程命令行，`ps aux` 人人可见 |
| `container run -e PRR_JITCONFIG` | 容器运行时把它写进 `containers/<name>/config.json`（644），**且删容器不删记录** |
| 拷进容器内的 tmpfs | `container cp` 报告成功，但文件**穿不进运行中 VM 的挂载点** |
| 走 stdin | `container run` **不转发 stdin** |

现在走的是 `container cp` 拷进容器文件系统：宿主侧只在 `mkstemp` 建的 0600 临时文件里过一下、
随即 unlink；容器侧由入口包装读完立刻删，宿主 `ps` 与容器记录里都不留、删容器无残留。


## 启动器的行为

池子模式：同时维持 `replicas` 个待命容器（每个一次性 runner 只接一个任务，所以并发 >1 才扛得住排队）。

起容器之前**两项自检**，任一不过就停掉待命容器且不再起新的、并报警：

- **出口自检（安全）**：起一个一次性容器，检查还能不能碰到宿主机上与本方案无关的服务端口 —— 碰得到说明 pf 那层不在了。
- **网关自检（可用性）**：两个端口有没有在监听 —— 不在的话容器会注册成功、领到任务、然后在审查时连不上模型，白吃一个任务。

补新槽之前还看**宿主内存水位**：可回收低于 `min_free_gb` 就不补。macOS 是先压缩后换页，不会崩，但审查是延迟敏感的。

每小时一次**定期清理**：删已停止的容器、删无引用的镜像（**只在池子非空时做**，否则会把自己的镜像清掉）、删"离线且容器已不存在"的 runner 记录。

> ⚠️ 容器在 `container system df` / `du` 里报的磁盘占用是**克隆+稀疏的账面数**，不是它独占的盘。
> 删掉容器不会释放镜像占用的空间，真正占盘的是**镜像堆积**（每次 CI 重建都会新增），
> 这也是定期清理针对的东西。看容量请用 `df`，别用 `container system df`。
