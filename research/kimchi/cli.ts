import { open, writeFile } from 'node:fs/promises'

async function readJson(path: string, limit: number): Promise<unknown> {
  const file = await open(path, 'r')
  try {
    const buffer = Buffer.alloc(limit + 1)
    let offset = 0
    while (offset < buffer.length) {
      const { bytesRead } = await file.read(buffer, offset, buffer.length - offset, null)
      if (bytesRead === 0) break
      offset += bytesRead
    }
    if (offset > limit) throw new Error('Proof data exceeds the size limit')
    return JSON.parse(buffer.toString('utf8', 0, offset))
  } finally {
    await file.close()
  }
}

function writeJson(path: string, data: unknown) {
  return writeFile(path, JSON.stringify(data, null, 2) + '\n', { flag: 'wx', mode: 0o600 })
}

async function main([action, ...args]: string[]) {
  switch (action) {
    case 'issue': {
      if (args.length !== 3 || !/^[0-3]$/.test(args[0])) break
      const { issue } = await import('./prover.ts')
      const { assignment, opening } = issue(Number(args[0]))
      await writeJson(args[2], opening)
      await writeJson(args[1], assignment)
      return
    }
    case 'prove': {
      if (args.length !== 4) break
      const { proveReceipt } = await import('./prover.ts')
      const { receipt, reply, compileMs, proveMs } = await proveReceipt(
        await readJson(args[0], 4096), await readJson(args[1], 1024),
      )
      await writeJson(args[3], reply)
      await writeJson(args[2], receipt)
      console.log(`Created four proofs in ${Math.round(proveMs)} ms (key setup ${Math.round(compileMs)} ms)`)
      return
    }
    case 'verify': {
      if (args.length < 2 || args.length > 3) break
      const { verifyReceipt } = await import('./receipt.ts')
      const assignment = await readJson(args[0], 4096)
      const receipt = await readJson(args[1], 262_144)
      const reply = args[2] ? await readJson(args[2], 1024) : undefined
      const started = performance.now()
      if (!await verifyReceipt(assignment, receipt, reply)) throw new Error('Invalid inference receipt')
      console.log(`Verified four linked proofs${reply === undefined ? '' : ' and both reply tokens'} in ${Math.round(performance.now() - started)} ms`)
      return
    }
    case 'export-key': {
      if (args.length !== 1) break
      const { compilePilot } = await import('./prover.ts')
      const { verificationKey } = await compilePilot()
      await writeJson(args[0], { data: verificationKey.data })
      return
    }
    case 'granite-export-key': {
      if (args.length !== 2) break
      const { exportGraniteKey } = await import('./granite-prover.ts')
      await writeJson(args[1], await exportGraniteKey(await readJson(args[0], 96_000)))
      return
    }
    case 'granite-issue': {
      if (args.length !== 3) break
      const { issueGraniteProjection } = await import('./granite-prover.ts')
      const { assignment, opening } = issueGraniteProjection(await readJson(args[0], 96_000))
      await writeJson(args[2], opening)
      await writeJson(args[1], assignment)
      return
    }
    case 'granite-prove': {
      if (args.length !== 4) break
      const { proveGraniteProjection } = await import('./granite-prover.ts')
      const { receipt, compileMs, proveMs } = await proveGraniteProjection(
        await readJson(args[0], 96_000), await readJson(args[1], 4096), await readJson(args[2], 1024),
      )
      await writeJson(args[3], receipt)
      console.log(`Proved one Granite projection row in ${Math.round(proveMs)} ms (key setup ${Math.round(compileMs)} ms)`)
      return
    }
    case 'granite-verify': {
      if (args.length !== 2) break
      const { verifyGraniteProjection } = await import('./granite-receipt.ts')
      const assignment = await readJson(args[0], 4096)
      const receipt = await readJson(args[1], 66_000)
      const start = performance.now()
      if (!await verifyGraniteProjection(assignment, receipt)) throw new Error('Invalid Granite projection receipt')
      console.log(`Verified one Granite projection row in ${Math.round(performance.now() - start)} ms`)
      return
    }
  }
  throw new Error([
    'Usage: npm run proof:pilot -- <command>',
    '  issue <token 0-3> <assignment> <private input>',
    '  prove <assignment> <private input> <receipt> <private reply>',
    '  verify <assignment> <receipt> [private reply]',
    '  export-key <new key file>',
    '  granite-export-key <sample> <new key file>',
    '  granite-issue <sample> <assignment> <opening>',
    '  granite-prove <sample> <assignment> <opening> <receipt>',
    '  granite-verify <assignment> <receipt>',
  ].join('\n'))
}

main(process.argv.slice(2)).catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : 'Proof command failed')
  process.exitCode = 1
})
