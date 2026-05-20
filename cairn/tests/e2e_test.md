
## E2E test

在最后**必须手动模拟整个操作流程**（通过api调用也好，浏览器模拟也可以），确保完整符合需求，否则就得改到测试通过为止，除非测试环境出现无法解决的问题（如中转api挂了）可以向用户报告。

测试完毕后关掉测试用的所有监听服务。

## 本机测试db

测试专用 SQLite DB：

`cairn/tests/e2e/cairn.e2e.db`

测试专用yaml：

`dispatch.dev.yaml`

这两个你都可根据需要自行做任何修改。

## 测试端口

常用的端口可能被用户占用，测试自行开其余高端口进行。

## 专用机器、中转站、端点

`docker exec -it pentestVM zsh` 这台机器任你使用，唯一禁区是 /home/kali/ctf 目录不能动。已经装好了pi，claude code，codex。可以写好ssh的凭据自己配置，然后配置成ssh 远程工作区。

可复用 SSH 凭据：

- 私钥：[pentestvm_v32_ed25519](/Users/littlefairy/dev-env/isolated_workspace/Cairn/cairn/tests/e2e/pentestvm_v32_ed25519)
- 公钥：[pentestvm_v32_ed25519.pub](/Users/littlefairy/dev-env/isolated_workspace/Cairn/cairn/tests/e2e/pentestvm_v32_ed25519.pub)
- 推荐本机 SSH config：`/tmp/cairn_pentestvm_v32_ssh_config`

准备命令：

```bash
chmod 600 cairn/tests/e2e/pentestvm_v32_ed25519
docker cp cairn/tests/e2e/pentestvm_v32_ed25519.pub pentestVM:/tmp/cairn_pentestvm_v32_ed25519.pub
docker exec pentestVM zsh -lc 'sudo ssh-keygen -A && mkdir -p /home/kali/.ssh /home/kali/cairn-workspaces /home/kali/.cairn/bin && chmod 700 /home/kali/.ssh && touch /home/kali/.ssh/authorized_keys && grep -qxF "$(cat /tmp/cairn_pentestvm_v32_ed25519.pub)" /home/kali/.ssh/authorized_keys || cat /tmp/cairn_pentestvm_v32_ed25519.pub >> /home/kali/.ssh/authorized_keys; chmod 600 /home/kali/.ssh/authorized_keys && chown -R kali:kali /home/kali/.ssh /home/kali/cairn-workspaces /home/kali/.cairn && pgrep -x sshd >/dev/null || sudo /usr/sbin/sshd'
cat > /tmp/cairn_pentestvm_v32_ssh_config <<'EOF'
Host cairn-pentestvm-v32
  HostName 127.0.0.1
  User kali
  IdentityFile /Users/littlefairy/dev-env/isolated_workspace/Cairn/cairn/tests/e2e/pentestvm_v32_ed25519
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  ProxyCommand docker exec -i pentestVM ncat 127.0.0.1 22
EOF
ssh -F /tmp/cairn_pentestvm_v32_ssh_config -o BatchMode=yes cairn-pentestvm-v32 'whoami; command -v pi; pi --version'
```

本机测试用的中转地址： http://host.docker.internal:3000, 同时提供openai response，openai-compatible，anthropic端点。 openai模型测试用 gpt-5.4, anthropic模型测试（如果需要）可用 claude-sonnet-4-6

测试专用apikey： sk-JJ51WM5mGQLNu3qBMqOULfFRZpMeMW4a4ZDO5Ep6rubmJopS
