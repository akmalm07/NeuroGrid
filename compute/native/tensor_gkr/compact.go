package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/bits"
	"os"
	"runtime"
	"sync"
	"time"

	"github.com/consensys/gnark-crypto/ecc/bn254"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr"
	"github.com/consensys/gnark-crypto/ecc/bn254/fr/polynomial"
	"github.com/consensys/gnark-crypto/fiat-shamir"
	gkr "github.com/consensys/gnark/gkr/bn254"
	"github.com/consensys/gnark/gkr/gkrapi"
)

const compactProtocol = "neurogrid-compact-q18-gkr-v1"

type modelCommitment struct {
	Scheme, Model, Operator, WeightSHA256, PointsSHA256 string
	Layer, K, N                                         int
}

func reportCompact(phase string, data map[string]any) error {
	data["phase"] = phase
	return json.NewEncoder(os.Stdout).Encode(data)
}

func compactScope() (scope, [32]byte, error) {
	s, encoded, err := loadScope()
	if err == nil && (s.M != 1 || s.K < 4 || s.N < 2 || s.K > 4096 || s.N > 4096 ||
		bits.OnesCount(uint(s.K)) != 1 || bits.OnesCount(uint(s.N)) != 1) {
		err = errors.New("compact projection requires one token and power-of-two dimensions")
	}
	return s, sha256.Sum256(encoded), err
}

func weightManifest(s scope, points []byte) modelCommitment {
	return modelCommitment{compactProtocol, s.Model, s.Operation, s.Right, digest(points), s.Layer, s.K, s.N}
}

func knownModel(s scope) ([]bn254.G1Affine, [32]byte, error) {
	var handle [32]byte
	encoded, err := file("model.commit", s.N*bn254.SizeOfG1AffineCompressed)
	if err != nil {
		return nil, handle, err
	}
	info, err := os.Stat(jobFile("model.json"))
	if err != nil || info.Size() > 1024 {
		return nil, handle, errors.New("invalid trusted model commitment metadata")
	}
	manifest, err := file("model.json", int(info.Size()))
	if err != nil {
		return nil, handle, err
	}
	expected, err := json.Marshal(weightManifest(s, encoded))
	if err != nil || !bytes.Equal(expected, manifest) {
		return nil, handle, errors.New("model commitment does not match the trusted projection")
	}
	points, err := readPoints(bytes.NewReader(encoded), s.N)
	return points, sha256.Sum256(expected), err
}

func lanes32(data []byte) ([]int32, error) {
	lanes := make([]int32, len(data)/4)
	for i := range lanes {
		lanes[i] = int32(binary.LittleEndian.Uint32(data[4*i:]))
		if lanes[i] <= -(1<<24) || lanes[i] >= 1<<24 {
			return nil, errors.New("lane outside signed Q18 input bounds")
		}
	}
	return lanes, nil
}

func verifiedWeights(s scope) ([]int32, error) {
	encoded, err := file("right.bin", s.K*s.N*4)
	if err != nil {
		return nil, err
	}
	if digest(encoded) != s.Right {
		return nil, errors.New("weight fixture differs from the pinned source")
	}
	return lanes32(encoded)
}

func verifiedActivation(s scope) (polynomial.MultiLin, error) {
	encoded, err := file("left.bin", s.K*4)
	if err != nil {
		return nil, err
	}
	if digest(encoded) != s.Left {
		return nil, errors.New("activation fixture differs from the issued scope")
	}
	lanes, err := lanes32(encoded)
	if err != nil {
		return nil, err
	}
	result := make(polynomial.MultiLin, len(lanes))
	for i := range lanes {
		result[i].SetInt64(int64(lanes[i]))
	}
	return result, nil
}

func parallelLanes(count int, work func(int) error) error {
	workers := min(count, runtime.GOMAXPROCS(0))
	var wait sync.WaitGroup
	failures := make(chan error, workers)
	for worker := range workers {
		wait.Go(func() {
			for i := worker; i < count; i += workers {
				if err := work(i); err != nil {
					failures <- err
					return
				}
			}
		})
	}
	wait.Wait()
	close(failures)
	return <-failures
}

func setupModel(s scope) error {
	start := time.Now()
	lanes, err := verifiedWeights(s)
	if err != nil {
		return err
	}
	g, err := generators(s.K)
	if err != nil {
		return err
	}
	points := make([]bn254.G1Affine, s.N)
	begin := time.Now()
	negative := make([]bn254.G1Affine, len(g))
	for i := range g {
		negative[i].Neg(&g[i])
	}
	err = parallelLanes(s.N, func(column int) error {
		values := make([]fr.Element, s.K)
		bases := make([]bn254.G1Affine, s.K)
		for row := range s.K {
			lane := lanes[row*s.N+column]
			if lane < 0 {
				bases[row] = negative[row]
				lane = -lane
			} else {
				bases[row] = g[row]
			}
			values[row].SetUint64(uint64(lane))
		}
		point, err := msm(bases, values)
		points[column] = point
		return err
	})
	if err != nil {
		return err
	}
	msmSeconds := time.Since(begin).Seconds()
	encoded := encodePoints(points)
	manifest, err := json.Marshal(weightManifest(s, encoded))
	if err != nil {
		return err
	}
	if err := os.WriteFile(jobFile("model.commit"), encoded, 0600); err != nil {
		return err
	}
	if err := os.WriteFile(jobFile("model.json"), manifest, 0600); err != nil {
		return err
	}
	return reportCompact("compact_model", map[string]any{"seconds": time.Since(start).Seconds(),
		"msm_seconds": msmSeconds, "commitment_bytes": len(encoded) + len(manifest), "pin": digest(manifest)})
}

func activationTag(scopeHash, modelHash [32]byte) [32]byte {
	return sha256.Sum256(bytes.Join([][]byte{[]byte(compactProtocol + "/activation\x00"), scopeHash[:], modelHash[:]}, nil))
}

func issueCompact(s scope, tag [32]byte, width int) error {
	start := time.Now()
	activation, err := verifiedActivation(s)
	if err != nil {
		return err
	}
	rows, err := commitRows(activation, width)
	if err != nil {
		return err
	}
	encoded := append(tag[:], encodePoints(rows)...)
	if err := os.WriteFile(jobFile("activation.commit"), encoded, 0600); err != nil {
		return err
	}
	return reportCompact("compact_issue", map[string]any{"seconds": time.Since(start).Seconds(),
		"commitment_bytes": len(encoded), "pin": digest(encoded)})
}

func boundStatement(tag [32]byte, activation []byte, rawHash, roundedHash fr.Element) [32]byte {
	raw, rounded := rawHash.Bytes(), roundedHash.Bytes()
	return sha256.Sum256(bytes.Join([][]byte{[]byte(compactProtocol + "/statement\x00"),
		tag[:], activation, raw[:], rounded[:]}, nil))
}

func reductionPoint(statement [32]byte, count int) []fr.Element {
	n := bits.Len(uint(count)) - 1
	names := make([]string, n)
	for i := range names {
		names[i] = fmt.Sprintf("%s/output/%d", compactProtocol, i)
	}
	transcript := fiatshamir.NewTranscript(sha512.New(), names...)
	_ = transcript.Bind(names[0], statement[:])
	result := make([]fr.Element, n)
	for i, name := range names {
		challenge, _ := transcript.ComputeChallenge(name)
		result[i].SetBytes(challenge)
	}
	return result
}

func compactClaims(s scope, statement [32]byte, want fr.Element, point []fr.Element, activation []bn254.G1Affine,
	weights []bn254.G1Affine, assignment gkr.WireAssignment, receipt *bytes.Reader) ([]byte, error) {
	api := gkrapi.New()
	x, y := api.NewInput(), api.NewInput()
	api.Export(api.Mul(x, y))
	circuit, schedule, err := api.Compile(gkrapi.PrimeField(fr.Modulus()), gkrapi.ConsolidateNone)
	if err != nil {
		return nil, err
	}
	if len(circuit) != 3 || x != 0 || y != 1 {
		return nil, errors.New("unexpected compact GKR gate layout")
	}
	logK := bits.Len(uint(s.K)) - 1
	rows := logK / 2
	width := 1 << (logK - rows)
	half := make([]fr.Element, logK)
	inverseTwo := fr.NewElement(2)
	inverseTwo.Inverse(&inverseTwo)
	for i := range half {
		half[i] = inverseTwo
	}
	var product []bn254.G1Affine
	var productCommit []byte
	var output bytes.Buffer
	var claims gkr.Claims
	var found fr.Element
	if assignment != nil {
		for i := range s.K {
			assignment[2][i].Mul(&assignment[0][i], &assignment[1][i])
			found.Add(&found, &assignment[2][i])
		}
		if !found.Equal(&want) {
			return nil, errors.New("matrix result differs from rescale accumulator")
		}
		product, err = commitRows(assignment[2], width)
		if err != nil {
			return nil, err
		}
		productCommit = encodePoints(product)
		output.Write(productCommit)
		proof, evaluations, err := gkr.Prove(circuit, schedule, assignment, seed(statement, productCommit))
		if err != nil {
			return nil, err
		}
		if err := evaluations.Check(assignment); err != nil {
			return nil, err
		}
		claims = evaluations
		var transcript bytes.Buffer
		for _, v := range proof.Flatten() {
			writeFields(&transcript, []fr.Element{*v})
		}
		_ = binary.Write(&output, binary.BigEndian, uint16(transcript.Len()/fr.Bytes))
		output.Write(transcript.Bytes())
		openVector(&output, assignment[2], rows, half)
		writeFields(&output, assignment[1])
	} else {
		product, err = readPoints(receipt, 1<<rows)
		if err != nil {
			return nil, err
		}
		productCommit = encodePoints(product)
		var size uint16
		if err := binary.Read(receipt, binary.BigEndian, &size); err != nil || int(size) != 2*logK+3 {
			return nil, errors.New("invalid GKR transcript length")
		}
		encoded, err := readFields(receipt, int(size))
		if err != nil {
			return nil, err
		}
		proof, err := gkr.DeserializeProof(circuit, schedule, encoded)
		if err != nil {
			return nil, err
		}
		claims, err = gkr.Verify(circuit, schedule, logK, proof, seed(statement, productCommit))
		if err != nil {
			return nil, err
		}
		found, err = authenticateVector(receipt, product, half)
		if err != nil {
			return nil, err
		}
		count := fr.NewElement(uint64(s.K))
		found.Mul(&found, &count)
		if !found.Equal(&want) {
			return nil, errors.New("authenticated GKR product sum differs from rescale evaluation")
		}
	}
	var weightFold polynomial.MultiLin
	if assignment == nil {
		weightFold, err = readFields(receipt, s.K)
		if err != nil {
			return nil, err
		}
		if err := authenticateFold(weights, point, weightFold); err != nil {
			return nil, err
		}
	}
	for wire := range 3 {
		if len(claims[wire]) == 0 {
			return nil, errors.New("missing GKR evaluation claim")
		}
		for _, claim := range claims[wire] {
			if len(claim.EvaluationPoint) != logK {
				return nil, errors.New("invalid GKR evaluation point")
			}
			if assignment != nil {
				if wire != 1 {
					openVector(&output, assignment[wire], rows, claim.EvaluationPoint)
				}
				continue
			}
			switch wire {
			case 0:
				found, err = authenticateVector(receipt, activation, claim.EvaluationPoint)
			case 1:
				found = weightFold.Evaluate(claim.EvaluationPoint, nil)
			case 2:
				found, err = authenticateVector(receipt, product, claim.EvaluationPoint)
			}
			if err != nil {
				return nil, err
			}
			if !found.Equal(&claim.Evaluation) {
				return nil, fmt.Errorf("authenticated opening differs from GKR claim %d", wire)
			}
		}
	}
	if receipt != nil && receipt.Len() != 0 {
		return nil, errors.New("trailing GKR proof data")
	}
	return output.Bytes(), nil
}

func runCompact(operation string) error {
	start := time.Now()
	s, scopeHash, err := compactScope()
	if err != nil {
		return err
	}
	switch operation {
	case "compact-model":
		return setupModel(s)
	case "compact-key":
		return setupRescale(s.N)
	}
	weights, modelHash, err := knownModel(s)
	if err != nil {
		return err
	}
	tag := activationTag(scopeHash, modelHash)
	rows := 1 << ((bits.Len(uint(s.K)) - 1) / 2)
	width := s.K / rows
	if operation == "compact-issue" {
		return issueCompact(s, tag, width)
	}
	activation, err := file("activation.commit", 32+rows*bn254.SizeOfG1AffineCompressed)
	if err != nil {
		return err
	}
	if !bytes.Equal(tag[:], activation[:32]) {
		return errors.New("activation commitment is for a different request or model")
	}
	activationRows, err := readPoints(bytes.NewReader(activation[32:]), rows)
	if err != nil {
		return err
	}
	if operation == "compact-verify" {
		return verifyCompact(s, start, tag, activation, activationRows, weights)
	}
	return proveCompact(s, start, tag, activation, width)
}

func proveCompact(s scope, start time.Time, tag [32]byte, activation []byte, width int) error {
	left, err := verifiedActivation(s)
	if err != nil {
		return err
	}
	computed, err := commitRows(left, width)
	if err != nil {
		return err
	}
	if !bytes.Equal(encodePoints(computed), activation[32:]) {
		return errors.New("activation commitment differs from the independently issued input")
	}
	right, err := verifiedWeights(s)
	if err != nil {
		return err
	}
	rawBytes, err := file("raw.bin", s.N*8)
	if err != nil {
		return err
	}
	roundedBytes, err := file("rounded.bin", s.N*8)
	if err != nil {
		return err
	}
	raw, rounded := make([]int64, s.N), make([]int64, s.N)
	for i := range raw {
		raw[i] = int64(binary.LittleEndian.Uint64(rawBytes[i*8:]))
		rounded[i] = int64(binary.LittleEndian.Uint64(roundedBytes[i*8:]))
		if raw[i] <= -accumulatorBound || raw[i] >= accumulatorBound || rounded[i] != roundQ18(raw[i]) {
			return errors.New("input fixture violates the bounded signed Q18 rounding rule")
		}
	}
	loadSeconds := time.Since(start).Seconds()
	begin := time.Now()
	rawHash := hashLanes(raw, rawDomain, 53, 4, accumulatorBound)
	roundedHash := hashLanes(rounded, roundedDomain, 36, 7, roundedOffset)
	statement := boundStatement(tag, activation, rawHash, roundedHash)
	coords := reductionPoint(statement, s.N)
	expected := evaluateRaw(raw, coords)
	commitSeconds := time.Since(begin).Seconds()
	begin = time.Now()
	basis := eqBasis(coords)
	folded := make(polynomial.MultiLin, s.K)
	_ = parallelLanes(s.K, func(row int) error {
		var term fr.Element
		for col := range s.N {
			term.SetInt64(int64(right[row*s.N+col])).Mul(&term, &basis[col])
			folded[row].Add(&folded[row], &term)
		}
		return nil
	})
	foldSeconds := time.Since(begin).Seconds()
	begin = time.Now()
	matrix, err := compactClaims(s, statement, expected, coords, nil, nil,
		gkr.WireAssignment{left, folded, make(polynomial.MultiLin, s.K)}, nil)
	if err != nil {
		return err
	}
	matrixSeconds := time.Since(begin).Seconds()
	bridge, keyLoad, rescaleSeconds, err := proveRescale(rescaleWitness(raw, coords, rawHash, roundedHash, expected))
	if err != nil {
		return err
	}
	begin = time.Now()
	var proof bytes.Buffer
	writeFields(&proof, []fr.Element{rawHash, roundedHash, expected})
	_ = binary.Write(&proof, binary.BigEndian, uint16(len(bridge)))
	proof.Write(bridge)
	proof.Write(matrix)
	if err := os.WriteFile(jobFile("projection.proof"), proof.Bytes(), 0600); err != nil {
		return err
	}
	return reportCompact("compact_prove", map[string]any{"seconds": time.Since(start).Seconds(),
		"input_seconds": loadSeconds, "commitment_seconds": commitSeconds, "fold_seconds": foldSeconds,
		"gkr_seconds": matrixSeconds, "key_load_seconds": keyLoad, "rescale_seconds": rescaleSeconds,
		"serialize_seconds": time.Since(begin).Seconds(), "proof_bytes": proof.Len(), "rescale_bytes": len(bridge),
		"scope_pin": hex.EncodeToString(tag[:])})
}

func verifyCompact(s scope, start time.Time, tag [32]byte, activation []byte, activationRows, weights []bn254.G1Affine) error {
	info, err := os.Stat(jobFile("projection.proof"))
	if err != nil || info.Size() < 100 || info.Size() >= 1<<20 {
		return errors.New("invalid projection proof size")
	}
	encoded, err := file("projection.proof", int(info.Size()))
	if err != nil {
		return err
	}
	reader := bytes.NewReader(encoded)
	header, err := readFields(reader, 3)
	if err != nil {
		return err
	}
	var n uint16
	if err := binary.Read(reader, binary.BigEndian, &n); err != nil || n == 0 || n > 512 || int(n) >= reader.Len() {
		return errors.New("invalid rescale proof length")
	}
	bridge := make([]byte, n)
	if _, err := reader.Read(bridge); err != nil {
		return err
	}
	statement := boundStatement(tag, activation, header[0], header[1])
	coords := reductionPoint(statement, s.N)
	begin := time.Now()
	if err := verifyRescale(bridge, rescaleWitness(nil, coords, header[0], header[1], header[2])); err != nil {
		return err
	}
	rescaleTime := time.Since(begin).Seconds()
	begin = time.Now()
	if _, err := compactClaims(s, statement, header[2], coords, activationRows, weights, nil, reader); err != nil {
		return err
	}
	openingSeconds := time.Since(begin).Seconds()
	if _, err := os.Stat(jobFile("returned-q18.bin")); err == nil {
		if err := verifyReturnedOutput(s.N, header[1]); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	roundedHash := header[1].Bytes()
	return reportCompact("compact_verify", map[string]any{"seconds": time.Since(start).Seconds(),
		"rescale_seconds": rescaleTime, "opening_seconds": openingSeconds,
		"proof_bytes": len(encoded), "rounded_commitment": hex.EncodeToString(roundedHash[:])})
}

func verifyReturnedOutput(count int, commitment fr.Element) error {
	encoded, err := file("returned-q18.bin", count*8)
	if err != nil {
		return err
	}
	lanes := make([]int64, count)
	for i := range lanes {
		lanes[i] = int64(binary.LittleEndian.Uint64(encoded[i*8:]))
		if lanes[i] < -(1<<34) || lanes[i] > 1<<34 {
			return errors.New("returned Q18 lane exceeds the rounding range")
		}
	}
	got := hashLanes(lanes, roundedDomain, 36, 7, roundedOffset)
	if !got.Equal(&commitment) {
		return errors.New("returned Q18 tensor does not match the proved output commitment")
	}
	return nil
}
