# OpenCloud + MinIO + OpenSearch

PoC piccolo: OpenCloud originale, MinIO come storage dei documenti, OpenSearch per i chunk vettoriali e un consumer Python esterno. Nessuna modifica o estensione a OpenCloud.

## Avvio

```bash
cd opencloud
bash scripts/start.sh
bash scripts/test.sh
```

Interfaccia: **https://localhost:9200**, utente `admin`, password `OC_ADMIN_PASSWORD` nel file locale `.env`. Il certificato è autofirmato: il browser richiede di accettarlo. Il PoC è esposto solo sulla macchina locale. Per usare un altro indirizzo occorre configurare `OC_URL` coerentemente anche per il login web.

MinIO e OpenSearch sono interni alla rete Docker. File, metadati OpenCloud e indice persistono nei volumi; credenziali, modello e stato del consumer si trovano in `.env` e `.private/`.

```bash
docker compose logs -f events    # eventi e sincronizzazioni
docker compose stop            # arresto conservando i dati
docker compose up -d            # riavvio
```

## Flusso

1. Caricamento dall'interfaccia OpenCloud o dalle sue API WebDAV. OpenCloud conserva i contenuti in MinIO tramite il driver nativo `decomposeds3`; i metadati restano nel volume OpenCloud.
2. OpenCloud pubblica gli eventi sul proprio broker NATS JetStream. Il consumer ha una sottoscrizione **push** persistente a `main-queue`, separata dai consumer interni di OpenCloud.
3. Il consumer legge il documento e i permessi dalle API native. Divide il testo in blocchi di 160 parole, calcola embedding multilingua reali da 384 dimensioni e scrive i chunk in OpenSearch.
4. Un cambiamento delle condivisioni o dei membri dei gruppi aggiorna `acl` e `allowed_user_ids` sui chunk già indicizzati. Se la versione è invariata, i vettori vengono conservati.
5. Nuova versione o ripristino generano una nuova sincronizzazione: nell'indice rimangono i chunk della versione corrente. La cronologia dei file è gestita da OpenCloud.

NATS fa parte di OpenCloud: non è un quinto servizio da configurare. Il consumer conferma gli eventi dopo averli elaborati e ritenta gli errori; non interroga periodicamente documenti o log. La conservazione nativa del bus è di sette giorni: il PoC non include una procedura di recupero per interruzioni più lunghe.

## Test

`test.sh` esegue cinque verifiche reali, creando tre piccoli documenti TXT, due utenti e due gruppi:

1. Caricamento tramite OpenCloud, download e verifica dei contenuti effettivamente presenti in MinIO.
2. Creazione automatica dei chunk tramite eventi e presenza dei vettori da 384 dimensioni.
3. Condivisioni ai gruppi, confronto fra accesso WebDAV e filtro nell'indice, modifica del ruolo, revoca e nuova concessione; rimozione e aggiunta di un membro al gruppo. Verifica che vettori e contatore di embedding non cambino.
4. Caricamento della seconda versione e sostituzione automatica dei chunk precedenti.
5. Ripristino tramite versioning nativo e riallineamento automatico dei chunk.

L'attesa nel test serve a osservare i risultati asincroni. Non è il meccanismo di ingestion.

Esiti in `evidence/test.log` e `evidence/test.json`. I documenti e gli account di prova restano visibili nella UI. Ogni esecuzione usa un nuovo suffisso; le credenziali dell'ultima coppia di utenti sono in `.private/events/test-users.json` e non vengono stampate nei log.

## Perimetro mantenuto semplice

Il consumer segue **solo file TXT UTF-8 nello spazio personale di admin**, usando le API con quell'account. È un test dell'integrazione: non una scansione di tutti gli spazi aziendali. Non include OCR, parsing PDF/Office, chat o un endpoint di ricerca RAG.

Le ACL testate sono proprietario e condivisioni dirette autenticate a utenti/gruppi con ruoli di lettura e modifica. Ereditarietà da cartelle, regole di diniego e spazi condivisi non fanno parte del collaudo. I gruppi sono espansi negli utenti autorizzati; i link pubblici non danno accesso ai chunk. Le modifiche dei membri devono passare dalle API/UI native di OpenCloud, che generano gli eventi; un aggiornamento diretto in un identity provider esterno non è coperto da questo test.

Le modifiche ACL causano una rilettura dei permessi dei soli documenti già conosciuti dal consumer. Questo è sufficiente per il piccolo PoC ed evita una scansione periodica del server. Il riallineamento è asincrono: `allowed_user_ids` dimostra il filtro, ma non sostituisce un controllo di accesso aggiornato al momento di servire risultati in un'applicazione RAG reale.

Non aggiungere file preesistenti prima dell'avvio del consumer: non è previsto un recupero iniziale tramite scansione. Non azzerare separatamente volumi Docker e `.private/events`, che rappresentano lo stesso ambiente di test.

## Riferimenti

- [OpenCloud 9.0.0, codice originale](https://github.com/opencloud-eu/opencloud/tree/v9.0.0), immagine ufficiale fissata per digest in `compose.yaml`.
- [Storage S3/MinIO nativo](https://docs.opencloud.eu/docs/admin/configuration/storage/storage-decomposeds3/).
- [Servizio eventi NATS](https://docs.opencloud.eu/docs/dev/server/services/nats/information/).
- [Eventi nativi: file, condivisioni e gruppi](https://github.com/opencloud-eu/reva/tree/v2.52.1/pkg/events).

Il modello ONNX è fissato a una revisione e verificato tramite hash da `scripts/fetch_model.py`, come nel PoC Mayan.
