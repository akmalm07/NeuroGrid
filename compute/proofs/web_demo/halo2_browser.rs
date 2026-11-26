//! Browser bindings built inside the pinned EZKL source tree.

use crate::circuit::modules::Module;
use crate::graph::{modules::ModulePoseidon, GraphCircuit, GraphSettings};
use crate::pfsys::{field_to_string, verify_proof_circuit, Snark};
use halo2_proofs::plonk::VerifyingKey;
use halo2_proofs::poly::commitment::Params;
use halo2_proofs::poly::kzg::{
    commitment::{KZGCommitmentScheme, ParamsKZG},
    multiopen::VerifierSHPLONK,
    strategy::SingleStrategy,
};
use halo2_proofs::SerdeFormat;
use halo2curves::bn256::{Bn256, Fr, G1Affine};
use halo2curves::ff::PrimeField;
use snark_verifier::system::halo2::transcript::evm::EvmTranscript;
use std::collections::HashMap;
use std::io::Cursor;
use wasm_bindgen::prelude::*;

fn failure(reason: impl std::fmt::Display) -> JsValue {
    JsValue::from_str(&reason.to_string())
}

fn fields(encoded: &[u8]) -> Result<Vec<Fr>, JsValue> {
    if encoded.is_empty() || encoded.len() % 32 != 0 || encoded.len() > 4096 {
        return Err(failure("Invalid instance length"));
    }
    encoded.chunks_exact(32).map(|chunk| {
        let mut repr = <Fr as PrimeField>::Repr::default();
        repr.as_mut().copy_from_slice(chunk);
        Option::from(Fr::from_repr(repr)).ok_or_else(|| failure("Invalid field element"))
    }).collect()
}

/// Compute an EZKL tensor commitment over canonical little-endian BN254 scalars.
#[wasm_bindgen]
pub fn tensor_commitment(encoded: &[u8]) -> Result<String, JsValue> {
    let hash = ModulePoseidon::run(fields(encoded)?).map_err(failure)?;
    Ok(field_to_string(&hash[0][0]))
}

/// Hold the trusted public Halo2 parameters and keys for the duration of a browser session.
#[derive(Debug)]
#[wasm_bindgen]
pub struct BrowserVerifier {
    params: ParamsKZG<Bn256>,
    keys: HashMap<String, (VerifyingKey<G1Affine>, u32)>,
}

#[wasm_bindgen]
impl BrowserVerifier {
    /// Load public KZG parameters.
    #[wasm_bindgen(constructor)]
    pub fn new(srs: &[u8]) -> Result<BrowserVerifier, JsValue> {
        let params = ParamsKZG::read(&mut Cursor::new(srs)).map_err(failure)?;
        Ok(Self {params, keys: HashMap::new()})
    }

    /// Install a locally authenticated circuit's verification key.
    pub fn add_key(&mut self, name: String, settings: &[u8], key: &[u8]) -> Result<(), JsValue> {
        let settings: GraphSettings = serde_json::from_slice(settings).map_err(failure)?;
        let logrows = settings.run_args.logrows;
        if !(1..=16).contains(&logrows) || logrows != self.params.k() || name.len() > 32 {
            return Err(failure("Invalid circuit parameters"));
        }
        let vk = VerifyingKey::<G1Affine>::read::<_, GraphCircuit>(
            &mut Cursor::new(key), SerdeFormat::RawBytes, settings,
        ).map_err(failure)?;
        self.keys.insert(name, (vk, logrows));
        Ok(())
    }

    /// Verify one SHPLONK proof using public instances reconstructed in the browser.
    pub fn verify_segment(&self, name: &str, encoded: &[u8], proof: &[u8]) -> Result<bool, JsValue> {
        let (vk, logrows) = self.keys.get(name).ok_or_else(|| failure("Unknown circuit key"))?;
        let snark = Snark::new(None, vec![fields(encoded)?], proof.to_vec(), None, None, None);
        let strategy = SingleStrategy::new(&self.params);
        Ok(verify_proof_circuit::<
            VerifierSHPLONK<'_, Bn256>,
            KZGCommitmentScheme<Bn256>,
            _,
            _,
            EvmTranscript<G1Affine, _, _, _>,
        >(&snark, &self.params, vk, strategy, 1u64 << logrows).is_ok())
    }
}
