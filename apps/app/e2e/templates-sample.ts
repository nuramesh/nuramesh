/** A small valid nuramesh.yaml for the import test. */
export const exportYamlSample = `version: 1
org:
  id: sample
  name: Sample
  roles:
    - id: cos
      name: Chief of Staff
      kind: chief_of_staff
    - id: dev
      name: Developer
  agents:
    - id: chief
      name: Chief
      role: cos
    - id: dana
      name: Dana
      role: dev
      reports_to: chief
`;
